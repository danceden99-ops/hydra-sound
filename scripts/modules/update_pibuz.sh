#!/usr/bin/env bash
set -euo pipefail
# Modular updater for Qobuz Connect (pibuz)
if command -v cargo >/dev/null 2>&1; then
  cargo install --git https://github.com/PhilipVinc/pibuz --force
  ln -sf "$HOME/.cargo/bin/pibuz" /usr/local/bin/pibuz
else
  echo "Rust/cargo not installed; install rustup to build latest pibuz." >&2
  exit 1
fi
