#!/usr/bin/env bash
#
# Compilation de l'APK blueco (Android) sans Android Studio.
#
#   ./scripts/build-android.sh                 # APK de debug
#   ./scripts/build-android.sh --release       # APK de release (non signé)
#   ./scripts/build-android.sh --install-sdk   # installe SDK + build-tools manquants
#   ./scripts/build-android.sh --adb           # installe l'APK sur l'appareil branché
#   ./scripts/build-android.sh --clean
#
# Prérequis : un JDK 17+, et le SDK Android (ou --install-sdk avec les
# command-line-tools). Gradle est téléchargé automatiquement s'il est absent.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ANDROID_DIR="$ROOT/android"
DIST_DIR="$ROOT/dist"
CACHE_DIR="${XDG_CACHE_HOME:-$HOME/.cache}/blueco"
GRADLE_VERSION="8.9"

COMPILE_SDK=35
BUILD_TOOLS="35.0.0"

BUILD_TYPE="debug"
DO_CLEAN=0
DO_INSTALL_SDK=0
DO_ADB=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --release) BUILD_TYPE="release" ;;
    --debug) BUILD_TYPE="debug" ;;
    --clean) DO_CLEAN=1 ;;
    --install-sdk) DO_INSTALL_SDK=1 ;;
    --adb) DO_ADB=1 ;;
    -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "option inconnue: $1" >&2; exit 2 ;;
  esac
  shift
done

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()   { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------------------- #
# 1. JDK
# --------------------------------------------------------------------------- #
if [[ -z "${JAVA_HOME:-}" ]]; then
  if command -v java >/dev/null 2>&1; then
    JAVA_HOME="$(dirname "$(dirname "$(readlink -f "$(command -v java)")")")"
  else
    die "aucun JDK trouvé. Sous Arch : sudo pacman -S jdk17-openjdk"
  fi
fi
JAVA_MAJOR="$("$JAVA_HOME/bin/java" -version 2>&1 | sed -n '1s/.*version "\([0-9]*\).*/\1/p')"
[[ -n "$JAVA_MAJOR" && "$JAVA_MAJOR" -ge 17 ]] || die "JDK 17+ requis (détecté: ${JAVA_MAJOR:-inconnu})"
export JAVA_HOME
info "JDK $JAVA_MAJOR ($JAVA_HOME)"

# --------------------------------------------------------------------------- #
# 2. SDK Android
# --------------------------------------------------------------------------- #
find_sdk() {
  local candidates=(
    "${ANDROID_SDK_ROOT:-}" "${ANDROID_HOME:-}"
    "$HOME/Android/Sdk" "$HOME/android-sdk" "/opt/android-sdk"
    "/usr/lib/android-sdk"
  )
  for candidate in "${candidates[@]}"; do
    [[ -n "$candidate" && -d "$candidate" ]] && { echo "$candidate"; return 0; }
  done
  return 1
}

if ! SDK_DIR="$(find_sdk)"; then
  die "SDK Android introuvable.
  Installez-le puis relancez, par exemple :
    sudo pacman -S android-sdk android-sdk-platform-tools   # Arch (AUR pour les build-tools)
  ou via les command-line-tools officiels :
    mkdir -p \$HOME/Android/Sdk/cmdline-tools && cd \$HOME/Android/Sdk/cmdline-tools
    unzip commandlinetools-linux-*.zip && mv cmdline-tools latest
    export ANDROID_SDK_ROOT=\$HOME/Android/Sdk
    sdkmanager \"platform-tools\" \"platforms;android-$COMPILE_SDK\" \"build-tools;$BUILD_TOOLS\""
fi

export ANDROID_SDK_ROOT="$SDK_DIR"
export ANDROID_HOME="$SDK_DIR"
info "SDK Android: $SDK_DIR"
echo "sdk.dir=$SDK_DIR" > "$ANDROID_DIR/local.properties"

have_platform() { [[ -d "$SDK_DIR/platforms/android-$COMPILE_SDK" ]]; }
have_build_tools() { [[ -d "$SDK_DIR/build-tools/$BUILD_TOOLS" ]]; }

SDKMANAGER=""
for candidate in "$SDK_DIR/cmdline-tools/latest/bin/sdkmanager" "$SDK_DIR/tools/bin/sdkmanager"; do
  [[ -x "$candidate" ]] && { SDKMANAGER="$candidate"; break; }
done

if ! have_platform || ! have_build_tools; then
  if [[ "$DO_INSTALL_SDK" -eq 1 && -n "$SDKMANAGER" ]]; then
    info "installation de platforms;android-$COMPILE_SDK et build-tools;$BUILD_TOOLS"
    yes | "$SDKMANAGER" --licenses >/dev/null || true
    "$SDKMANAGER" "platform-tools" "platforms;android-$COMPILE_SDK" "build-tools;$BUILD_TOOLS"
  else
    warn "SDK incomplet (platforms;android-$COMPILE_SDK ou build-tools;$BUILD_TOOLS manquant)."
    if [[ -n "$SDKMANAGER" ]]; then
      warn "relancez avec --install-sdk, ou :"
      warn "  \"$SDKMANAGER\" \"platforms;android-$COMPILE_SDK\" \"build-tools;$BUILD_TOOLS\""
    else
      warn "sdkmanager introuvable : installez les command-line-tools puis --install-sdk"
    fi
    exit 1
  fi
fi

# --------------------------------------------------------------------------- #
# 3. Gradle
# --------------------------------------------------------------------------- #
GRADLE_BIN=""
if [[ -x "$ANDROID_DIR/gradlew" ]]; then
  GRADLE_BIN="$ANDROID_DIR/gradlew"
elif command -v gradle >/dev/null 2>&1; then
  GRADLE_BIN="$(command -v gradle)"
else
  GRADLE_HOME="$CACHE_DIR/gradle-$GRADLE_VERSION"
  if [[ ! -x "$GRADLE_HOME/bin/gradle" ]]; then
    mkdir -p "$CACHE_DIR"
    ZIP="$CACHE_DIR/gradle-$GRADLE_VERSION-bin.zip"
    if [[ ! -f "$ZIP" ]]; then
      info "téléchargement de Gradle $GRADLE_VERSION"
      if command -v curl >/dev/null 2>&1; then
        curl -fsSL -o "$ZIP" "https://services.gradle.org/distributions/gradle-$GRADLE_VERSION-bin.zip"
      else
        wget -qO "$ZIP" "https://services.gradle.org/distributions/gradle-$GRADLE_VERSION-bin.zip"
      fi
    fi
    info "décompression de Gradle"
    unzip -q -o "$ZIP" -d "$CACHE_DIR"
  fi
  GRADLE_BIN="$GRADLE_HOME/bin/gradle"
fi
info "Gradle: $GRADLE_BIN"

# --------------------------------------------------------------------------- #
# 4. Compilation
# --------------------------------------------------------------------------- #
cd "$ANDROID_DIR"
if [[ "$DO_CLEAN" -eq 1 ]]; then
  info "nettoyage"
  "$GRADLE_BIN" --no-daemon clean
fi

TASK="assembleDebug"
[[ "$BUILD_TYPE" == "release" ]] && TASK="assembleRelease"
info "compilation de :app:$TASK"
"$GRADLE_BIN" --no-daemon "$TASK"

APK_SRC="$ANDROID_DIR/app/build/outputs/apk/$BUILD_TYPE/app-$BUILD_TYPE.apk"
[[ -f "$APK_SRC" ]] || APK_SRC="$(find "$ANDROID_DIR/app/build/outputs/apk/$BUILD_TYPE" -name '*.apk' | head -1)"
[[ -f "$APK_SRC" ]] || die "APK introuvable après compilation"

VERSION="$(sed -n 's/.*versionName *= *"\([^"]*\)".*/\1/p' "$ANDROID_DIR/app/build.gradle.kts" | head -1)"
mkdir -p "$DIST_DIR"
APK_DST="$DIST_DIR/blueco-$VERSION-$BUILD_TYPE.apk"
cp "$APK_SRC" "$APK_DST"
info "APK prêt : $APK_DST"

if [[ "$DO_ADB" -eq 1 ]]; then
  ADB="$(command -v adb || echo "$SDK_DIR/platform-tools/adb")"
  [[ -x "$ADB" ]] || die "adb introuvable"
  info "installation sur l'appareil"
  "$ADB" install -r "$APK_DST"
  APP_ID="dev.blueco.app"
  [[ "$BUILD_TYPE" == "debug" ]] && APP_ID="dev.blueco.app.debug"
  "$ADB" shell monkey -p "$APP_ID" -c android.intent.category.LAUNCHER 1 >/dev/null
  info "lancement de $APP_ID"
fi

cat <<EOF

Prochaine étape (appairage et configuration) :
  1. appairez le téléphone avec le PC :  bluetoothctl  →  pair <MAC>  →  trust <MAC>
  2. sur le PC :  blueco init --peer-mac <MAC_DU_TELEPHONE> --headphones-mac <MAC_DU_CASQUE>
  3. sur le PC :  blueco serve -v
  4. dans l'appli : renseignez « Adresse MAC du PC » puis « Démarrer le service »
EOF
