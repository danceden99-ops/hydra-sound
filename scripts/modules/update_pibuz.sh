#!/usr/bin/env bash
set -euo pipefail
# Modular installer/updater for Qobuz Connect (pibuz)
ASSET_URL="https://github.com/danceden99-ops/hydra-sound/releases/latest/download/hydra-sound-arm64-binaries.tar.gz"
SHA_URL="${ASSET_URL}.sha256"

if [[ "${PREFER_SOURCE:-0}" != "1" ]] && [[ "$(uname -m)" == "aarch64" ]]; then
  TMP_DIR=$(mktemp -d)
  if curl -fsSL "$ASSET_URL" -o "$TMP_DIR/hydra-sound-arm64-binaries.tar.gz" \
    && curl -fsSL "$SHA_URL" -o "$TMP_DIR/hydra-sound-arm64-binaries.tar.gz.sha256" \
    && (cd "$TMP_DIR" && sha256sum -c hydra-sound-arm64-binaries.tar.gz.sha256); then
    tar -xzf "$TMP_DIR/hydra-sound-arm64-binaries.tar.gz" -C /usr/local/bin pibuz
    chmod +x /usr/local/bin/pibuz
    rm -rf "$TMP_DIR"
    echo "Installed prebuilt pibuz binary."
    exit 0
  fi
  rm -rf "$TMP_DIR"
fi

if ! command -v cargo >/dev/null 2>&1; then
  apt-get update && apt-get install -y cargo rustc libasound2-dev pkg-config
fi
CARGO_BIN_DIR="${CARGO_HOME:-$HOME/.cargo}/bin"
cargo install --git https://github.com/PhilipVinc/pibuz --force
ln -sf "$CARGO_BIN_DIR/pibuz" /usr/local/bin/pibuz
