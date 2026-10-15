#!/usr/bin/env bash
#
# Installe une chaîne de compilation Android complète DANS L'ESPACE UTILISATEUR
# (aucun sudo, rien hors de ~/.cache/blueco) :
#
#   ~/.cache/blueco/jdk-17         JDK Temurin 17
#   ~/.cache/blueco/android-sdk    SDK : platform-tools, android-35, build-tools 35
#   ~/.cache/blueco/gradle-8.9     Gradle
#
#   ./scripts/setup-android-toolchain.sh          # installe ce qui manque
#   ./scripts/setup-android-toolchain.sh --check  # dit seulement ce qui manque
#
# Ensuite : ./scripts/build-android.sh (il détecte automatiquement cette chaîne).

set -euo pipefail

CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/blueco"
JDK_DIR="$CACHE/jdk-17"
SDK_DIR="$CACHE/android-sdk"
GRADLE_VERSION="8.9"
GRADLE_DIR="$CACHE/gradle-$GRADLE_VERSION"
TMP="$CACHE/tmp"

COMPILE_SDK=35
BUILD_TOOLS="35.0.0"

CMDLINE_TOOLS_URL="https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip"
JDK_URL="https://api.adoptium.net/v3/binary/latest/17/ga/linux/x64/jdk/hotspot/normal/eclipse"
GRADLE_URL="https://services.gradle.org/distributions/gradle-$GRADLE_VERSION-bin.zip"

CHECK_ONLY=0
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=1

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

fetch() {  # fetch <url> <destination>
  local url="$1" dest="$2"
  if [[ -s "$dest" ]]; then
    info "déjà téléchargé : $(basename "$dest")"
    return 0
  fi
  info "téléchargement de $(basename "$dest")"
  mkdir -p "$(dirname "$dest")"
  if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 3 --progress-bar -o "$dest.part" "$url"
  elif command -v wget >/dev/null 2>&1; then
    wget -q --show-progress -O "$dest.part" "$url"
  else
    die "ni curl ni wget disponible"
  fi
  mv "$dest.part" "$dest"
}

extract_zip() {  # extract_zip <archive> <destination>
  local archive="$1" dest="$2"
  if command -v unzip >/dev/null 2>&1; then
    unzip -q -o "$archive" -d "$dest"
  else
    python3 -m zipfile -e "$archive" "$dest"
  fi
}

missing=()
[[ -x "$JDK_DIR/bin/java" ]] || missing+=("JDK 17")
[[ -x "$SDK_DIR/platform-tools/adb" ]] || missing+=("SDK platform-tools")
[[ -d "$SDK_DIR/platforms/android-$COMPILE_SDK" ]] || missing+=("SDK platforms;android-$COMPILE_SDK")
[[ -d "$SDK_DIR/build-tools/$BUILD_TOOLS" ]] || missing+=("SDK build-tools;$BUILD_TOOLS")
[[ -x "$GRADLE_DIR/bin/gradle" ]] || missing+=("Gradle $GRADLE_VERSION")

if [[ ${#missing[@]} -eq 0 ]]; then
  info "chaîne complète : aucune installation nécessaire"
  exit 0
fi

warn "manquant : ${missing[*]}"
if [[ "$CHECK_ONLY" -eq 1 ]]; then
  exit 1
fi

command -v python3 >/dev/null 2>&1 || die "python3 est requis pour décompresser sans unzip"
mkdir -p "$TMP" "$CACHE"
avail_kb="$(df -Pk "$CACHE" | awk 'NR==2 {print $4}')"
if [[ "$avail_kb" -lt 4000000 ]]; then
  warn "moins de 4 Go libres dans $CACHE : l'installation pourrait échouer"
fi

# --------------------------------------------------------------------------- #
# 1. JDK 17 (Temurin)
# --------------------------------------------------------------------------- #
if [[ ! -x "$JDK_DIR/bin/java" ]]; then
  fetch "$JDK_URL" "$TMP/jdk17.tar.gz"
  info "décompression du JDK"
  rm -rf "$TMP/jdk"
  mkdir -p "$TMP/jdk"
  tar -xzf "$TMP/jdk17.tar.gz" -C "$TMP/jdk"
  inner="$(find "$TMP/jdk" -maxdepth 1 -mindepth 1 -type d | head -1)"
  [[ -n "$inner" ]] || die "archive du JDK illisible"
  rm -rf "$JDK_DIR"
  mv "$inner" "$JDK_DIR"
  rm -rf "$TMP/jdk" "$TMP/jdk17.tar.gz"
fi
export JAVA_HOME="$JDK_DIR"
export PATH="$JDK_DIR/bin:$PATH"
info "JDK : $("$JDK_DIR/bin/java" -version 2>&1 | head -1)"

# --------------------------------------------------------------------------- #
# 2. SDK Android (cmdline-tools puis sdkmanager)
# --------------------------------------------------------------------------- #
SDKMANAGER="$SDK_DIR/cmdline-tools/latest/bin/sdkmanager"
if [[ ! -x "$SDKMANAGER" ]]; then
  fetch "$CMDLINE_TOOLS_URL" "$TMP/cmdline-tools.zip"
  info "installation de cmdline-tools"
  rm -rf "$TMP/cmdline-tools"
  mkdir -p "$TMP/cmdline-tools/cmdline-tools"
  extract_zip "$TMP/cmdline-tools.zip" "$TMP/cmdline-tools"
  mkdir -p "$SDK_DIR/cmdline-tools"
  rm -rf "$SDK_DIR/cmdline-tools/latest"
  mv "$TMP/cmdline-tools/cmdline-tools" "$SDK_DIR/cmdline-tools/latest"
  rm -rf "$TMP/cmdline-tools"
  chmod +x "$SDKMANAGER"
fi

repos="$SDK_DIR"
export ANDROID_SDK_ROOT="$SDK_DIR"
export ANDROID_HOME="$SDK_DIR"
mkdir -p "$repos"

info "acceptation des licences du SDK"
yes | "$SDKMANAGER" --sdk_root="$repos" --licenses >/dev/null 2>&1 || true

info "installation de platform-tools, platforms;android-$COMPILE_SDK et build-tools;$BUILD_TOOLS"
"$SDKMANAGER" --sdk_root="$repos" --install \
  "platform-tools" "platforms;android-$COMPILE_SDK" "build-tools;$BUILD_TOOLS"

# --------------------------------------------------------------------------- #
# 3. Gradle
# --------------------------------------------------------------------------- #
if [[ ! -x "$GRADLE_DIR/bin/gradle" ]]; then
  fetch "$GRADLE_URL" "$TMP/gradle.zip"
  info "décompression de Gradle"
  rm -rf "$GRADLE_DIR"
  extract_zip "$TMP/gradle.zip" "$CACHE"
  rm -f "$TMP/gradle.zip"
fi

rm -rf "$TMP"

info "terminé"
cat <<EOF

Chaîne Android installée en espace utilisateur :
  JDK     : $JDK_DIR
  SDK     : $SDK_DIR
  Gradle  : $GRADLE_DIR

Compilez et installez l'appli :
  ./scripts/build-android.sh --adb
EOF
