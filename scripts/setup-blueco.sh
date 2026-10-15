#!/usr/bin/env bash
#
# Configuration automatique de blueco, de bout en bout.
#
#   ./scripts/setup-blueco.sh                 # TOUT : téléphone branché en USB, rien d'autre à faire
#   ./scripts/setup-blueco.sh --test          # + vérifie une vraie bascule (aller et retour)
#   ./scripts/setup-blueco.sh --no-build      # ne compile/réinstalle pas l'appli
#   ./scripts/setup-blueco.sh --no-app        # ne touche pas aux réglages de l'appli
#   ./scripts/setup-blueco.sh --power         # accepte d'utiliser sudo (paquets Arch manquants)
#   ./scripts/setup-blueco.sh --dry-run       # montre ce qui serait fait
#
# Ce que le script trouve tout seul :
#   * l'adresse Bluetooth du PC           (BlueZ / D-Bus)
#   * l'adresse Bluetooth du téléphone    (adb: settings secure bluetooth_address)
#   * les écouteurs en cours d'usage      (adb: dumpsys bluetooth_manager → appareil A2DP actif)
#   * la chaîne de compilation Android    (téléchargée dans ~/.cache si absente)
#
# Ce qu'il fait, dans l'ordre :
#   1. vérifie Python/BlueZ (et les paquets Arch manquants si --power)
#   2. lit ce que le téléphone sait de lui-même
#   3. lit ce que le PC sait de lui-même
#   4. compile et installe l'appli si besoin, puis pousse sa configuration
#   5. appaire le téléphone au PC si nécessaire (et marque les appareils « de confiance »)
#   6. écrit la configuration du PC, installe le lanceur + le service et le démarre
#   7. vérifie que la liaison Bluetooth est bien montée
#
# L'appli sait aussi se débrouiller seule (elle retrouve le PC et les écouteurs
# parmi les appareils appairés) : ce script ne sert qu'à aller plus vite et à
# faire l'appairage à ta place.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_PATH="${XDG_CONFIG_HOME:-$HOME/.config}/blueco/config.json"
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/blueco"
DAEMON_LOG="${XDG_STATE_HOME:-$HOME/.local/state}/blueco/daemon.log"
DEBUG_PKG="dev.blueco.app.debug"
RELEASE_PKG="dev.blueco.app"
SPP_UUID="00001101-0000-1000-8000-00805f9b34fb"

DO_TEST=0
DO_BUILD=1
DO_APP_PREFS=1
DO_POWER=0
DO_DRY=0
HEADPHONES_OVERRIDE=""
PEER_NAME_OVERRIDE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --test) DO_TEST=1 ;;
    --no-build) DO_BUILD=0 ;;
    --no-app) DO_APP_PREFS=0 ;;
    --power) DO_POWER=1 ;;
    --headphones) HEADPHONES_OVERRIDE="${2:-}"; shift ;;
    --peer-name) PEER_NAME_OVERRIDE="${2:-}"; shift ;;
    --dry-run) DO_DRY=1 ;;
    -h|--help) sed -n '2,30p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "option inconnue: $1" >&2; exit 2 ;;
  esac
  shift
done

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m[✓]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()   { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }
step()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
run()   { if [[ "$DO_DRY" -eq 1 ]]; then info "[--dry-run] $*"; else "$@"; fi; }

py() { PYTHONPATH="$ROOT" python3 -c "$1"; }

# L'adaptateur du PC est lu avant le téléphone : on en a besoin pour ne pas
# confondre les écouteurs avec le PC dans ce que raconte Android.
PC_MAC=""
PC_NAME=""
read_pc_adapter() {
  local info
  info="$(py "
from blueco.bluez import open_client, BluezError
try:
    adapter = open_client('auto').adapter_info() or {}
    print(adapter.get('address', ''), adapter.get('name', ''))
except BluezError:
    print('')
" 2>/dev/null || true)"
  PC_MAC="$(printf '%s' "$info" | awk '{print $1}')"
  PC_NAME="$(printf '%s' "$info" | awk '{print $2}')"
  PC_NAME="${PC_NAME:-$(hostname)}"
}

# Adresse mémorisée par une exécution précédente (certaines ROM masquent la
# leur et l'appairage serait alors impossible à viser).
previous_peer_mac() {
  py "
import json, pathlib
try:
    cfg = json.loads(pathlib.Path('$CONFIG_PATH').read_text())
    peers = cfg.get('peers') or []
    print(peers[0].get('mac', '') if peers else '')
except Exception:
    print('')
" 2>/dev/null || true
}

BT_DEVICES_JSON='[]'
refresh_pc_devices() {
  BT_DEVICES_JSON="$(py "
import json
from blueco.bluez import open_client, BluezError
try:
    print(json.dumps([d.as_dict() for d in open_client('auto').devices()]))
except BluezError:
    print('[]')
" 2>/dev/null || echo '[]')"
}

known_device() {  # known_device <MAC> → ligne JSON de l'appareil tel que vu par le PC
  printf '%s' "$BT_DEVICES_JSON" | python3 -c "
import json, sys
mac = sys.argv[1].upper()
for device in json.load(sys.stdin):
    if device['mac'].upper() == mac:
        print(json.dumps(device))
        break
" "$1" 2>/dev/null || true
}

is_paired_on_pc() {
  local line; line="$(known_device "$1")"
  [[ -n "$line" ]] && printf '%s' "$line" | grep -q '"paired": true'
}

is_trusted_on_pc() {
  local line; line="$(known_device "$1")"
  [[ -n "$line" ]] && printf '%s' "$line" | grep -q '"trusted": true'
}

# --------------------------------------------------------------------------- #
# 1. Dépendances
# --------------------------------------------------------------------------- #
step "1/7  Dépendances (Python, BlueZ)"

MISSING_DEPS=()
command -v python3 >/dev/null 2>&1 || MISSING_DEPS+=("python")
py "import dbus" 2>/dev/null || MISSING_DEPS+=("python-dbus")
py "import gi" 2>/dev/null || MISSING_DEPS+=("python-gobject")
command -v bluetoothctl >/dev/null 2>&1 || MISSING_DEPS+=("bluez-utils")
systemctl is-active --quiet bluetooth 2>/dev/null || MISSING_DEPS+=("bluetooth.service")

if [[ ${#MISSING_DEPS[@]} -gt 0 ]]; then
  warn "manquant : ${MISSING_DEPS[*]}"
  if [[ "$DO_POWER" -eq 1 ]] && command -v pacman >/dev/null 2>&1; then
    run sudo pacman -S --needed --noconfirm python python-dbus python-gobject bluez bluez-utils
    run sudo systemctl enable --now bluetooth
  else
    echo "    relance avec --power pour que je l'installe, ou lance à la main :"
    echo "      sudo pacman -S --needed python python-dbus python-gobject bluez bluez-utils"
    echo "      sudo systemctl enable --now bluetooth"
  fi
else
  ok "Python + D-Bus + BlueZ présents, service bluetooth actif"
fi

if ! systemctl is-active --quiet bluetooth 2>/dev/null; then
  warn "bluetoothd n'est pas actif : la bascule ne peut pas fonctionner"
  warn "lance : sudo systemctl enable --now bluetooth"
fi

# --------------------------------------------------------------------------- #
# 2. Téléphone branché en USB
# --------------------------------------------------------------------------- #
step "2/7  Téléphone (USB)"

read_pc_adapter

find_adb() {
  local candidates=(
    "$(command -v adb 2>/dev/null || true)"
    "${ANDROID_SDK_ROOT:-}/platform-tools/adb"
    "${ANDROID_HOME:-}/platform-tools/adb"
    "${XDG_CACHE_HOME:-$HOME/.cache}/blueco/android-sdk/platform-tools/adb"
    "$HOME/Android/Sdk/platform-tools/adb"
  )
  local candidate
  for candidate in "${candidates[@]}"; do
    [[ -n "$candidate" && -x "$candidate" ]] && { echo "$candidate"; return 0; }
  done
  return 1
}

ADB="$(find_adb || true)"
if [[ -z "$ADB" && "$DO_BUILD" -eq 1 ]]; then
  info "aucune chaîne Android : je la télécharge (sans sudo, dans ~/.cache/blueco)"
  run "$ROOT/scripts/setup-android-toolchain.sh"
  ADB="$(find_adb || true)"
fi
if [[ -z "$ADB" ]]; then
  warn "adb introuvable : impossible de configurer le téléphone automatiquement"
  warn "installe-le : ./scripts/setup-android-toolchain.sh"
fi

PHONE_MAC=""
PHONE_NAME=""
MODEL=""
HEADPHONES=""

if [[ -n "$ADB" ]]; then
  "$ADB" start-server >/dev/null 2>&1 || true
  STATE="$("$ADB" devices | awk 'NR==2 {print $2}')"
  case "${STATE:-}" in
    device) ;;
    "") die "aucun téléphone détecté : branche-le en USB et active le débogage USB (Options développeur)" ;;
    *)  die "téléphone en état « $STATE » : accepte « Autoriser le débogage USB » sur son écran, puis relance" ;;
  esac
  MODEL="$("$ADB" shell getprop ro.product.model 2>/dev/null | tr -d '\r')"
  ok "téléphone détecté : $MODEL (Android $("$ADB" shell getprop ro.build.version.release 2>/dev/null | tr -d '\r'))"

  DUMP="$("$ADB" shell dumpsys bluetooth_manager 2>/dev/null || true)"
  PHONE_MAC="$("$ADB" shell settings get secure bluetooth_address 2>/dev/null | tr -d '\r' || true)"
  if [[ ! "$PHONE_MAC" =~ ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$ ]]; then
    # Certaines ROM masquent l'adresse ici : on la relit dans le dump BlueZ.
    PHONE_MAC="$(printf '%s\n' "$DUMP" | grep -m1 -iE '^\s*address:' | grep -oE '([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}' || true)"
  fi
  PHONE_NAME="${PEER_NAME_OVERRIDE:-$("$ADB" shell settings get global device_name 2>/dev/null | tr -d '\r' || true)}"
  [[ -n "$PHONE_NAME" && "$PHONE_NAME" != "null" ]] || PHONE_NAME="$MODEL"
  if [[ -z "$PHONE_MAC" ]]; then
    PHONE_MAC="$(previous_peer_mac)"
    [[ -n "$PHONE_MAC" ]] && ok "adresse du téléphone reprise de la configuration : $PHONE_MAC"
  fi

  if [[ "$PHONE_MAC" =~ ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$ ]]; then
    PHONE_MAC="$(printf '%s' "$PHONE_MAC" | tr 'a-f' 'A-F')"
    ok "adresse du téléphone : $PHONE_MAC ($PHONE_NAME)"
  else
    PHONE_MAC=""
    warn "adresse du téléphone illisible sur cette ROM : appairage automatique impossible"
  fi

  # Écouteurs : les appareils audio connus du téléphone, repérés par la fin de
  # leur adresse (Android masque le début ailleurs), **en excluant le PC** : le
  # téléphone se connecte aussi en A2DP au PC après l'appairage, et il devient
  # alors son périphérique audio « actif ».
  BONDED="$(printf '%s\n' "$DUMP" | grep -E '^\s+([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\s*:' || true)"
  CANDIDATES="$(printf '%s\n' "$DUMP" \
    | grep -oE 'A2dpStateMachine for ([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}|A2dpStateMachine for ([0-9A-Fa-fX]{2}:){5}[0-9A-Fa-fX]{2}' \
    | awk '{print $NF}' | sort -u || true)"
  HEADPHONES="${HEADPHONES_OVERRIDE:-}"
  if [[ -z "$HEADPHONES" ]]; then
    # la configuration existante fait foi pour lever toute ambiguïté
    PREV_HP="$(py "
import json, pathlib
try:
    cfg = json.loads(pathlib.Path('$CONFIG_PATH').read_text())
    print((cfg.get('headphones') or {}).get('mac', '') or '')
except Exception:
    print('')
" 2>/dev/null || true)"
    if [[ -n "$PREV_HP" ]] && printf '%s\n' "$CANDIDATES" | grep -qiE "$(printf '%s' "$PREV_HP" | grep -oE '..:..$')$"; then
      HEADPHONES="$PREV_HP"
    fi
  fi
  if [[ -z "$HEADPHONES" ]]; then
    while IFS= read -r masked; do
      [[ -n "$masked" ]] || continue
      suffix="$(printf '%s' "$masked" | grep -oE '..:..$' || true)"
      [[ -n "$suffix" ]] || continue
      full="$(printf '%s\n' "$BONDED" | awk -v s="$suffix" '$1 ~ s {print $1; exit}' || true)"
      [[ -n "$full" ]] || continue
      # ni le PC, ni le téléphone lui-même
      [[ -n "${PC_MAC:-}" && "$full" == "$PC_MAC" ]] && continue
      [[ -n "$PHONE_MAC" && "$full" == "$PHONE_MAC" ]] && continue
      HEADPHONES="$full"
      break
    done <<< "$CANDIDATES"
  fi
  if [[ -n "$HEADPHONES" ]]; then
    HP_NAME="$(printf '%s\n' "$BONDED" | awk -v m="$HEADPHONES" '$1 == m {print $3; exit}')"
    ok "écouteurs utilisés par le téléphone : $HEADPHONES ${HP_NAME:+($HP_NAME)}"
  else
    warn "aucun écouteur identifié sur le téléphone"
    if [[ -n "$BONDED" ]]; then
      echo "    appareils appairés au téléphone :"
      printf '%s\n' "$BONDED" | awk '{printf "      %s  %s\n", $1, $3}'
      echo "    relance avec : ./scripts/setup-blueco.sh --headphones AA:BB:CC:DD:EE:FF"
    fi
  fi
fi

# --------------------------------------------------------------------------- #
# 3. Le PC
# --------------------------------------------------------------------------- #
step "3/7  PC (adaptateur Bluetooth)"

if [[ ! "$PC_MAC" =~ ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$ ]]; then
  die "adaptateur Bluetooth du PC illisible (bluetoothd démarré ? adaptateur présent ?)"
fi
ok "adaptateur du PC : $PC_MAC ($PC_NAME)"
refresh_pc_devices

# --------------------------------------------------------------------------- #
# 4. L'appli sur le téléphone
# --------------------------------------------------------------------------- #
step "4/7  Appli sur le téléphone"

PKG=""
if [[ -n "$ADB" ]]; then
  if "$ADB" shell pm list packages 2>/dev/null | grep -q "^package:$DEBUG_PKG$"; then
    PKG="$DEBUG_PKG"
  elif "$ADB" shell pm list packages 2>/dev/null | grep -q "^package:$RELEASE_PKG$"; then
    PKG="$RELEASE_PKG"
  fi
fi

if [[ -z "$PKG" && "$DO_BUILD" -eq 1 && -n "$ADB" ]]; then
  info "l'appli n'est pas installée : je la compile et l'installe (≈1 min)"
  run "$ROOT/scripts/build-android.sh" --debug --adb >/dev/null || die "échec de la compilation de l'appli"
  PKG="$DEBUG_PKG"
  ok "appli installée ($PKG)"
elif [[ -n "$PKG" ]]; then
  ok "appli déjà installée ($PKG)"
elif [[ -z "$ADB" ]]; then
  warn "pas d'adb : l'appli doit être installée à la main (./scripts/build-android.sh --adb)"
else
  warn "appli absente et compilation désactivée (--no-build)"
fi

if [[ -n "$PKG" && "$DO_APP_PREFS" -eq 1 ]]; then
  PREFS_XML="$(cat <<EOF
<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<map>
    <string name="peer_mac">${PC_MAC}</string>
    <string name="peer_name">${PC_NAME}</string>
    <string name="headphones_mac">${HEADPHONES}</string>
    <string name="headphones_name"></string>
    <string name="token"></string>
    <long name="retry_ms" value="5000" />
    <string name="device_name">${PHONE_NAME:-téléphone}</string>
    <boolean name="auto_start" value="true" />
</map>
EOF
)"
  if [[ "$DO_DRY" -eq 1 ]]; then
    info "[--dry-run] réglages qui seraient poussés dans $PKG :"
    printf '%s\n' "$PREFS_XML"
  else
    # L'appli doit être arrêtée avant l'écriture : sinon elle garde ses valeurs
    # en mémoire et réécrit le fichier par-dessus les nôtres. L'écriture passe
    # par run-as (build debug) pour se faire sous l'uid de l'appli.
    "$ADB" shell am force-stop "$PKG" >/dev/null 2>&1 || true
    sleep 1
    if printf '%s\n' "$PREFS_XML" | "$ADB" shell "run-as $PKG sh -c 'mkdir -p shared_prefs && cat > shared_prefs/blueco.xml'" 2>/dev/null; then
      ok "appli configurée (PC=$PC_MAC, écouteurs=${HEADPHONES:-?})"
    else
      warn "écriture directe impossible (build release) : l'appli se configurera toute seule"
      warn "au premier lancement, en lisant les appareils appairés"
    fi
    "$ADB" shell am start -n "$PKG/dev.blueco.app.MainActivity" >/dev/null 2>&1 || true
    ok "appli relancée"
  fi
fi

# --------------------------------------------------------------------------- #
# 5. Appairage
# --------------------------------------------------------------------------- #
step "5/7  Appairages Bluetooth"

fix_trust() {  # fix_trust <MAC> <libellé>
  local mac="$1" label="$2"
  [[ -n "$mac" ]] || return 0
  is_paired_on_pc "$mac" || return 0
  is_trusted_on_pc "$mac" && return 0
  # Un appareil appairé mais non « de confiance » est régulièrement refusé par
  # BlueZ : on marque la confiance nous-mêmes (réversible : bluetoothctl untrust).
  if [[ "$DO_DRY" -eq 1 ]]; then
    info "[--dry-run] bluetoothctl trust $mac"
  elif bluetoothctl --timeout 10 trust "$mac" >/dev/null 2>&1; then
    ok "$label marqué « de confiance »"
  else
    warn "impossible de marquer $label de confiance (bluetoothctl trust $mac)"
  fi
}

# Le PC se rend découvrable/appariable : sans ça, le téléphone ne le voit pas.
pc_discoverable() {
  [[ "$DO_DRY" -eq 1 ]] && { info "[--dry-run] bluetoothctl discoverable on / pairable on"; return 0; }
  { echo "power on"; echo "discoverable on"; echo "pairable on"; sleep 1; echo "quit"; } \
    | bluetoothctl >/dev/null 2>&1 || true
}

# Certaines ROM affichent la demande sur l'écran : on tente de la valider, et
# sinon on demande simplement à l'utilisateur de toucher « Associer ».
try_accept_on_phone() {
  [[ -n "$ADB" ]] || return 1
  "$ADB" shell input keyevent KEYCODE_WAKEUP >/dev/null 2>&1 || true
  "$ADB" shell svc power stayon true >/dev/null 2>&1 || true
  local dump="/sdcard/blueco-ui.xml" xml bounds
  "$ADB" shell uiautomator dump "$dump" >/dev/null 2>&1 || return 1
  xml="$("$ADB" shell cat "$dump" 2>/dev/null || true)"
  [[ -n "$xml" ]] || return 1
  bounds="$(printf '%s' "$xml" | tr '>' '\n' | grep -iE 'text="(associer|pair|appairer|ok|confirmer)"' \
    | grep -oE 'bounds="\[[0-9]+,[0-9]+\]\[[0-9]+,[0-9]+\]"' | head -1 || true)"
  [[ -n "$bounds" ]] || return 1
  local numbers x1 y1 x2 y2
  numbers="$(printf '%s' "$bounds" | grep -oE '[0-9]+' | tr '\n' ' ')"
  read -r x1 y1 x2 y2 <<< "$numbers"
  [[ -n "${x2:-}" ]] || return 1
  "$ADB" shell input tap "$(( (x1 + x2) / 2 ))" "$(( (y1 + y2) / 2 ))" >/dev/null 2>&1 || return 1
  return 0
}

pair_phone() {
  [[ -n "$PHONE_MAC" ]] || { warn "adresse du téléphone inconnue : appairage à faire à la main"; return 1; }
  pc_discoverable
  if [[ -n "$ADB" ]]; then
    # Écran Bluetooth ouvert = le téléphone devient découvrable (120 s).
    "$ADB" shell am start -a android.settings.BLUETOOTH_SETTINGS >/dev/null 2>&1 || true
    sleep 2
  fi
  info "le PC cherche le téléphone…"
  { echo "scan on"; sleep 10; echo "scan off"; sleep 1; echo "quit"; } | bluetoothctl >/dev/null 2>&1 || true
  refresh_pc_devices

  cat <<EOF

  ┌──────────────────────────────────────────────────────────────────────┐
  │  Un seul geste est demandé, sur le téléphone :                        │
  │                                                                      │
  │     touche « Associer » quand « $PC_NAME » apparaît                  │
  │                                                                      │
  │  (je réessaie tout seul pendant 2 minutes)                            │
  └──────────────────────────────────────────────────────────────────────┘

EOF

  local attempt
  for attempt in 1 2 3 4 5 6; do
    {
      echo "agent NoInputNoOutput"
      echo "default-agent"
      echo "pair $PHONE_MAC"
      sleep 12
      echo "yes"
      sleep 14
      echo "trust $PHONE_MAC"
      sleep 1
      echo "quit"
    } | bluetoothctl >/dev/null 2>&1 &
    local btpair=$!
    sleep 6
    if try_accept_on_phone; then
      ok "demande d'appairage validée automatiquement sur le téléphone"
    fi
    wait "$btpair" 2>/dev/null || true
    refresh_pc_devices
    if is_paired_on_pc "$PHONE_MAC"; then
      ok "téléphone appairé au PC"
      return 0
    fi
    info "tentative $attempt/6 : pas encore appairé…"
  done
  warn "appairage non abouti — relance ce script quand tu es prêt à toucher « Associer »"
  return 1
}

pair_headphones() {
  [[ -n "$HEADPHONES" ]] || { warn "écouteurs inconnus : appairage à faire à la main"; return 1; }
  cat <<EOF

  Les écouteurs (${HEADPHONES}) ne sont pas appairés au PC.
  Il faut les mettre en mode appairage : maintiens leur bouton jusqu'à ce que
  la LED clignote (débranche-les d'abord de tout le reste).

EOF
  read -r -p "  Appuie sur Entrée quand c'est fait (Ctrl-C pour plus tard) : " _ignored || true
  { echo "scan on"; sleep 8; echo "pair $HEADPHONES"; sleep 15; echo "trust $HEADPHONES"; sleep 1; echo "quit"; } \
    | bluetoothctl >/dev/null 2>&1 || true
  refresh_pc_devices
  if is_paired_on_pc "$HEADPHONES"; then
    ok "écouteurs appairés et de confiance"
    return 0
  fi
  warn "appairage des écouteurs non abouti"
  return 1
}

if [[ -n "$PHONE_MAC" ]]; then
  if is_paired_on_pc "$PHONE_MAC"; then
    ok "téléphone déjà appairé au PC"
    fix_trust "$PHONE_MAC" "téléphone"
  else
    pair_phone || true
    fix_trust "$PHONE_MAC" "téléphone"
  fi
fi

if [[ -n "$HEADPHONES" ]]; then
  if is_paired_on_pc "$HEADPHONES"; then
    ok "écouteurs déjà appairés au PC"
    fix_trust "$HEADPHONES" "écouteurs"
  else
    warn "écouteurs non appairés au PC : le PC ne pourra pas s'y connecter"
    if [[ -t 0 ]]; then pair_headphones || true; else
      warn "lance ce script dans un terminal pour que je puisse te guider"
    fi
    fix_trust "$HEADPHONES" "écouteurs"
  fi
fi

# --------------------------------------------------------------------------- #
# 6. Configuration du PC
# --------------------------------------------------------------------------- #
step "6/7  Configuration du PC"

APP_CONFIG="$(mktemp)"
cat > "$APP_CONFIG" <<EOF
{
  "device_name": "$PC_NAME",
  "token": "",
  "headphones": {
    "mac": "${HEADPHONES}",
    "name": ""
  },
  "peers": [
    {
      "name": "${PHONE_NAME:-téléphone}",
      "mac": "${PHONE_MAC}",
      "bluetooth_name": "${PHONE_NAME:-téléphone}",
      "mode": "profile1",
      "channel": 3,
      "token": "",
      "outbound": false
    }
  ],
  "transport": {
    "mode": "profile1",
    "channel": 3,
    "socket_path": "",
    "service_name": "blueco",
    "uuid": "${SPP_UUID}",
    "service_record": "",
    "control_socket": "",
    "require_authentication": true
  },
  "audio_backend": "bluez",
  "request_timeout_s": 15.0,
  "lock_command": "loginctl lock-session"
}
EOF

if [[ "$DO_DRY" -eq 1 ]]; then
  info "[--dry-run] configuration qui serait écrite dans $CONFIG_PATH :"
  cat "$APP_CONFIG"
  rm -f "$APP_CONFIG"
else
  mkdir -p "$(dirname "$CONFIG_PATH")"
  [[ -f "$CONFIG_PATH" ]] && cp "$CONFIG_PATH" "$CONFIG_PATH.bak"
  mv "$APP_CONFIG" "$CONFIG_PATH"
  chmod 600 "$CONFIG_PATH"
  if PYTHONPATH="$ROOT" python3 -c "
from blueco.config import Config
cfg = Config.load('$CONFIG_PATH')
cfg.validate()
print('    casque :', cfg.headphones.mac or '(non détecté)')
for peer in cfg.peers:
    print('    pair   :', peer.label(), peer.mac or '(résolu par BlueZ)')
"; then
    ok "configuration écrite dans $CONFIG_PATH"
  else
    die "configuration invalide — l'ancienne est dans $CONFIG_PATH.bak"
  fi
fi

# lanceur + unité systemd si nécessaire
if [[ ! -x "${XDG_BIN_HOME:-$HOME/.local/bin}/blueco" && "$DO_DRY" -eq 0 ]]; then
  info "installation du lanceur et du service (~30 s)"
  "$ROOT/scripts/build-linux.sh" >/dev/null 2>&1 && ok "lanceur et service installés" \
    || warn "installation partielle : relance ./scripts/build-linux.sh à la main"
fi

# démarrage du démon
UNIT_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/blueco.service"

# Un seul démon à la fois : deux démons se disputent l'enregistrement du profil
# SPP, et le téléphone se connecte alors à celui qu'on n'interroge pas.
stop_stray_daemons() {
  [[ "$DO_DRY" -eq 1 ]] && return 0
  local pids
  pids="$(pgrep -f 'python3 -m blueco serve' 2>/dev/null || true)"
  [[ -z "$pids" ]] && return 0
  info "arrêt des démons déjà lancés ($(printf '%s' "$pids" | tr '\n' ' '))"
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null || true
  sleep 2
  pids="$(pgrep -f 'python3 -m blueco serve' 2>/dev/null || true)"
  # shellcheck disable=SC2086
  [[ -n "$pids" ]] && kill -9 $pids 2>/dev/null || true
  return 0
}

if [[ "$DO_DRY" -eq 0 ]]; then
  if [[ -f "$UNIT_FILE" ]]; then
    run systemctl --user stop blueco.service 2>/dev/null || true
  fi
  stop_stray_daemons
  if [[ -f "$UNIT_FILE" ]] && systemctl --user restart blueco.service 2>/dev/null; then
    ok "service blueco (re)démarré"
  else
    mkdir -p "$(dirname "$DAEMON_LOG")"
    (cd "$ROOT" && nohup python3 -m blueco serve -v >>"$DAEMON_LOG" 2>&1 &)
    ok "démon lancé en arrière-plan (journal : $DAEMON_LOG)"
  fi
  sleep 2
  RUNNING="$(pgrep -fc 'python3 -m blueco serve' 2>/dev/null || echo 0)"
  [[ "$RUNNING" -eq 1 ]] || warn "$RUNNING démons tournent alors qu'un seul devrait (démon en double ?)"
fi

# --------------------------------------------------------------------------- #
# 7. Vérification
# --------------------------------------------------------------------------- #
step "7/7  Vérification de la liaison"

blueco_cmd() {  # blueco_cmd <secondes> <arguments…>
  local limit="$1"; shift
  ( cd "$ROOT" && PYTHONPATH="$ROOT" timeout "$limit" python3 -m blueco "$@" 2>&1 )
}

if [[ "$DO_DRY" -eq 0 ]]; then
  if [[ -n "$ADB" && -n "${PKG:-}" ]]; then
    info "réveil de l'appli pour qu'elle ouvre la liaison"
    "$ADB" shell am start -n "$PKG/dev.blueco.app.MainActivity" >/dev/null 2>&1 || true
  fi
  # Android relance l'appli lentement après un arrêt forcé et refait sa
  # connexion en arrière-plan : on laisse 2 minutes, en la réveillant de temps
  # en temps (chaque réveil redemande aussi l'ouverture du lien).
  info "attente de la liaison Bluetooth (le téléphone se reconnecte tout seul, 2 min max)…"
  LINKED=0
  for step in $(seq 1 60); do
    sleep 2
    if blueco_cmd 40 status --all --json 2>/dev/null | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if any(p.get('reachable') for p in data.get('peers', [])) else 1)
" 2>/dev/null; then
      LINKED=1
      break
    fi
    # réveil toutes les 10 s : inutile (et bavard) d'insister plus souvent
    if [[ $((step % 5)) -eq 0 && -n "$ADB" && -n "${PKG:-}" ]]; then
      "$ADB" shell am start -n "$PKG/dev.blueco.app.MainActivity" >/dev/null 2>&1 || true
      "$ADB" shell am broadcast -a dev.blueco.app.START \
        -n "$PKG/dev.blueco.app.TestReceiver" >/dev/null 2>&1 || true
    fi
  done

  echo
  info "état des appareils :"
  blueco_cmd 40 status --all | sed 's/^/    /' || true
  if [[ "$LINKED" -eq 1 ]]; then
    ok "liaison Bluetooth montée avec le téléphone"
  else
    warn "liaison non montée : vérifie l'appairage et que l'appli tourne (journal : $DAEMON_LOG)"
  fi

  if [[ "$DO_TEST" -eq 1 ]]; then
    echo
    info "test réel : je récupère les écouteurs sur ce PC"
    blueco_cmd 90 take | sed 's/^/    /' || true
    if [[ -n "${PHONE_NAME:-}" ]]; then
      echo
      info "test réel : je les rends au téléphone"
      blueco_cmd 90 give --to "$PHONE_NAME" | sed 's/^/    /' || true
    fi
  fi
fi

cat <<EOF

────────────────────────────────────────────────────────────────────────
Terminé.

  Écouteurs   : ${HEADPHONES:-inconnu}
  Téléphone   : ${PHONE_NAME:-inconnu} ${PHONE_MAC:+($PHONE_MAC)}
  PC          : ${PC_NAME} (${PC_MAC})
  Config      : ${CONFIG_PATH}

Depuis ce PC :
  blueco status                 qui tient les écouteurs, et où
  blueco take                   je récupère les écouteurs (le téléphone les lâche)
  blueco give --to "$PHONE_NAME"  je les rends au téléphone

Sur le téléphone : ouvre blueco et touche le gros bouton. Il bascule dans les
deux sens et démarre tout seul au démarrage du téléphone.
────────────────────────────────────────────────────────────────────────
EOF
