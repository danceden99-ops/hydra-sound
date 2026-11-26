#!/usr/bin/env bash
set -euo pipefail
# Modular installer/updater for Spotify Connect (go-librespot + librespot fallback)
ASSET_URL="https://github.com/danceden99-ops/hydra-sound/releases/latest/download/hydra-sound-arm64-binaries.tar.gz"
SHA_URL="${ASSET_URL}.sha256"

if [[ "${PREFER_SOURCE:-0}" != "1" ]] && [[ "$(uname -m)" == "aarch64" ]]; then
  apt-get update && apt-get install -y libogg0 libvorbis0a libvorbisfile3 libmpg123-0 >/dev/null 2>&1 || true
  apt-get install -y libflac12 >/dev/null 2>&1 || apt-get install -y libflac14 >/dev/null 2>&1 || true
  TMP_DIR=$(mktemp -d)
  if curl -fsSL "$ASSET_URL" -o "$TMP_DIR/hydra-sound-arm64-binaries.tar.gz"; then
    if curl -fsSL "$SHA_URL" -o "$TMP_DIR/hydra-sound-arm64-binaries.tar.gz.sha256" 2>/dev/null; then
      (cd "$TMP_DIR" && sha256sum -c hydra-sound-arm64-binaries.tar.gz.sha256)
    fi
    tar -xzf "$TMP_DIR/hydra-sound-arm64-binaries.tar.gz" -C /usr/local/bin
    chmod +x /usr/local/bin/librespot /usr/local/bin/go-librespot 2>/dev/null || true
    if [[ ! -s /usr/local/bin/go-librespot.version ]]; then
      curl -fsSL https://api.github.com/repos/devgianlu/go-librespot/releases/latest \
        | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' > /usr/local/bin/go-librespot.version || true
    fi
    rm -rf "$TMP_DIR"
    echo "Installed prebuilt Spotify Connect binaries."
    exit 0
  fi
  rm -rf "$TMP_DIR"
fi

if ! command -v cargo >/dev/null 2>&1; then
  apt-get update && apt-get install -y cargo rustc libasound2-dev pkg-config
fi
CARGO_BIN_DIR="${CARGO_HOME:-$HOME/.cargo}/bin"
cargo install --git https://github.com/librespot-org/librespot --branch dev --locked --force --no-default-features --features "alsa-backend,rustls-tls-native-roots,with-libmdns"
ln -sf "$CARGO_BIN_DIR/librespot" /usr/local/bin/librespot
