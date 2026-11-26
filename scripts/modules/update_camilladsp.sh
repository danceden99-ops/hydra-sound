#!/usr/bin/env bash
set -euo pipefail
# Modular installer/updater for CamillaDSP (ARM64 prebuilt release)
ARCH="$(uname -m)"
if [[ "$ARCH" != "aarch64" ]]; then
  echo "Unsupported architecture for prebuilt CamillaDSP: $ARCH" >&2
  exit 1
fi

CAMILLA_URL=$(curl -fsSL https://api.github.com/repos/HEnquist/camilladsp/releases/latest \
  | grep -o "https://[^\"]*linux-aarch64\\.tar\\.gz" | head -n 1 || true)

if [[ -z "$CAMILLA_URL" ]]; then
  echo "Could not find CamillaDSP linux-aarch64 release asset." >&2
  exit 1
fi

TMP_DIR=$(mktemp -d)
curl -fsSL "$CAMILLA_URL" -o "$TMP_DIR/camilladsp.tar.gz"
tar -xzf "$TMP_DIR/camilladsp.tar.gz" -C /usr/local/bin camilladsp
chmod +x /usr/local/bin/camilladsp
rm -rf "$TMP_DIR"
echo "Updated CamillaDSP: $(camilladsp --version 2>&1 | head -n 1)"
