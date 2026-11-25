#!/usr/bin/env bash
set -euo pipefail
# Modular installer/updater for Spotify Connect (librespot)
if ! command -v cargo >/dev/null 2>&1; then
  apt-get update && apt-get install -y cargo rustc libasound2-dev pkg-config
fi
CARGO_BIN_DIR="${CARGO_HOME:-$HOME/.cargo}/bin"
cargo install librespot --locked --force --no-default-features --features "alsa-backend,rustls-tls-native-roots"
ln -sf "$CARGO_BIN_DIR/librespot" /usr/local/bin/librespot
