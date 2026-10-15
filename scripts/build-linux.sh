#!/usr/bin/env bash
#
# Installation de blueco sur Arch Linux (et dérivés).
#
#   ./scripts/build-linux.sh                 # vérifie, installe le lanceur + unité systemd
#   ./scripts/build-linux.sh --deps          # installe les paquets Arch nécessaires (sudo)
#   ./scripts/build-linux.sh --test          # lance seulement les tests
#   ./scripts/build-linux.sh --systemd       # active et démarre le service utilisateur
#   ./scripts/build-linux.sh --watch         # idem pour la surveillance de proximité
#   ./scripts/build-linux.sh --pkg           # construit/installe le paquet pacman (makepkg)
#   ./scripts/build-linux.sh --uninstall
#
# Aucun venv n'est créé : sous Arch, python-dbus et python-gobject sont des
# paquets système, un environnement virtuel les masquerait.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${PREFIX:-$HOME/.local}"
BIN_DIR="$PREFIX/bin"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/blueco"

DO_DEPS=0
DO_TEST=0
DO_SYSTEMD=0
DO_WATCH=0
DO_PKG=0
DO_UNINSTALL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --deps) DO_DEPS=1 ;;
    --test) DO_TEST=1 ;;
    --systemd) DO_SYSTEMD=1 ;;
    --watch) DO_WATCH=1 ;;
    --pkg) DO_PKG=1 ;;
    --uninstall) DO_UNINSTALL=1 ;;
    -h|--help) sed -n '2,22p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "option inconnue: $1" >&2; exit 2 ;;
  esac
  shift
done

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------- #
# Dépendances système
# --------------------------------------------------------------------------- #
if [[ "$DO_DEPS" -eq 1 ]]; then
  command -v pacman >/dev/null 2>&1 || die "pacman introuvable : ce script cible Arch Linux"
  info "installation des paquets: python python-dbus python-gobject bluez bluez-utils"
  sudo pacman -S --needed --noconfirm python python-dbus python-gobject bluez bluez-utils
fi

check_deps() {
  local missing=()
  command -v python3 >/dev/null 2>&1 || missing+=("python")
  python3 -c "import dbus" 2>/dev/null || missing+=("python-dbus")
  python3 -c "import gi" 2>/dev/null || missing+=("python-gobject")
  command -v bluetoothctl >/dev/null 2>&1 || missing+=("bluez-utils")
  if [[ ${#missing[@]} -gt 0 ]]; then
    warn "paquets manquants: ${missing[*]}"
    warn "lancez : ./scripts/build-linux.sh --deps"
    return 1
  fi
  return 0
}

# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
run_tests() {
  info "tests unitaires et d'intégration"
  (cd "$ROOT" && python3 -m unittest discover -s tests -t .)
  info "auto-test de bout en bout (socket locale, backend simulé)"
  (cd "$ROOT" && python3 -m blueco --fake selftest)
}

if [[ "$DO_TEST" -eq 1 ]]; then
  run_tests
  exit 0
fi

# --------------------------------------------------------------------------- #
# Paquet pacman
# --------------------------------------------------------------------------- #
if [[ "$DO_PKG" -eq 1 ]]; then
  command -v makepkg >/dev/null 2>&1 || die "makepkg introuvable (paquet base-devel)"
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT
  cp "$ROOT/packaging/PKGBUILD" "$work/PKGBUILD"
  info "construction du paquet dans $work"
  (cd "$work" && BLUECO_SRC="$ROOT" makepkg -f)
  info "installation du paquet"
  (cd "$work" && BLUECO_SRC="$ROOT" makepkg -i --noconfirm)
  exit 0
fi

# --------------------------------------------------------------------------- #
# Désinstallation
# --------------------------------------------------------------------------- #
if [[ "$DO_UNINSTALL" -eq 1 ]]; then
  info "désinstallation"
  systemctl --user disable --now blueco.service 2>/dev/null || true
  systemctl --user disable --now blueco-watch.service 2>/dev/null || true
  rm -f "$UNIT_DIR/blueco.service" "$UNIT_DIR/blueco-watch.service" "$BIN_DIR/blueco"
  systemctl --user daemon-reload 2>/dev/null || true
  info "binaire et unités supprimés (la configuration $CONFIG_DIR est conservée)"
  exit 0
fi

# --------------------------------------------------------------------------- #
# Installation du lanceur et des unités systemd
# --------------------------------------------------------------------------- #
check_deps || exit 1

mkdir -p "$BIN_DIR" "$UNIT_DIR"

info "écriture du lanceur $BIN_DIR/blueco"
cat > "$BIN_DIR/blueco" <<EOF
#!/bin/sh
# Lanceur généré par scripts/build-linux.sh
PYTHONPATH="$ROOT\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m blueco "\$@"
EOF
chmod 0755 "$BIN_DIR/blueco"

for unit in blueco.service blueco-watch.service; do
  info "écriture de $UNIT_DIR/$unit"
  sed -e "s|@BIN@|$BIN_DIR/blueco|g" "$ROOT/packaging/$unit" > "$UNIT_DIR/$unit"
done

if [[ ! -f "$CONFIG_DIR/config.json" ]]; then
  mkdir -p "$CONFIG_DIR"
  info "création d'une configuration de départ: $CONFIG_DIR/config.json"
  cp "$ROOT/packaging/config.example.json" "$CONFIG_DIR/config.json"
  chmod 600 "$CONFIG_DIR/config.json"
else
  info "configuration existante conservée: $CONFIG_DIR/config.json"
fi

systemctl --user daemon-reload 2>/dev/null || warn "systemctl --user indisponible : unités écrites mais non rechargées"

info "vérification rapide"
"$BIN_DIR/blueco" --version
[[ -r "$ROOT/packaging/blueco.sudoers" ]] && info "verrouillage: voir $ROOT/packaging/blueco.sudoers si votre commande demande root"

if [[ "$DO_SYSTEMD" -eq 1 ]]; then
  info "activation du service blueco"
  systemctl --user enable --now blueco.service
  systemctl --user --no-pager --lines=10 status blueco.service || true
fi

if [[ "$DO_WATCH" -eq 1 ]]; then
  info "activation de la surveillance de proximité"
  systemctl --user enable --now blueco-watch.service
  systemctl --user --no-pager --lines=10 status blueco-watch.service || true
fi

cat <<EOF

Installation terminée. Étapes suivantes :

  1. appairage (une seule fois)
       bluetoothctl
       > scan on
       > pair <MAC_TELEPHONE>      # puis trust <MAC_TELEPHONE>
       > pair <MAC_CASQUE>         # puis trust <MAC_CASQUE>
       > scan off

  2. configuration
       blueco devices                            # liste des adresses disponibles
       blueco init --peer-mac <MAC_TELEPHONE> --headphones-mac <MAC_CASQUE>

  3. lancement du démon
       blueco serve -v
     ou en service utilisateur :  systemctl --user enable --now blueco

  4. côté téléphone : appli blueco, renseignez l'adresse MAC du PC, « Démarrer le service »

  Rappel : après un appairage, vérifiez que le casque est autorisé à se reconnecter
  (bluetoothctl → trust <MAC_CASQUE>), sinon il refusera la reprise depuis le PC.
EOF
