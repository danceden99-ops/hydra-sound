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

# Also install/update alsa_cdsp ALSA plugin from Hydra Sound release bundle
HYDRA_ASSET="https://github.com/danceden99-ops/hydra-sound/releases/latest/download/hydra-sound-arm64-binaries.tar.gz"
if curl -fsSL "$HYDRA_ASSET" -o "$TMP_DIR/bins.tar.gz" \
  && curl -fsSL "${HYDRA_ASSET}.sha256" -o "$TMP_DIR/bins.tar.gz.sha256" \
  && (cd "$TMP_DIR" && sha256sum -c bins.tar.gz.sha256 2>/dev/null || sha256sum -c <(sed 's/hydra-sound-arm64-binaries\.tar\.gz/bins.tar.gz/' bins.tar.gz.sha256)); then
  mkdir -p /usr/lib/aarch64-linux-gnu/alsa-lib
  tar -xzf "$TMP_DIR/bins.tar.gz" -C /usr/lib/aarch64-linux-gnu/alsa-lib libasound_module_pcm_cdsp.so 2>/dev/null || true
fi

rm -rf "$TMP_DIR"
echo "Updated CamillaDSP: $(camilladsp --version 2>&1 | head -n 1)"
