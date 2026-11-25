#!/usr/bin/env bash
set -euo pipefail
# Modular updater for Spotify Connect (librespot)
if command -v cargo >/dev/null 2>&1; then
  cargo install librespot --locked --force --no-default-features --features "alsa-backend"
  ln -sf "$HOME/.cargo/bin/librespot" /usr/local/bin/librespot
else
  echo "Rust/cargo not installed; install rustup to build latest librespot." >&2
  exit 1
fi
