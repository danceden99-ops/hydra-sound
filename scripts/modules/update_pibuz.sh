#!/usr/bin/env bash
set -euo pipefail
# Modular installer/updater for Qobuz Connect (pibuz)
if ! command -v cargo >/dev/null 2>&1; then
  apt-get update && apt-get install -y cargo rustc libasound2-dev pkg-config
fi
CARGO_BIN_DIR="${CARGO_HOME:-$HOME/.cargo}/bin"
cargo install --git https://github.com/PhilipVinc/pibuz --force
ln -sf "$CARGO_BIN_DIR/pibuz" /usr/local/bin/pibuz
