#!/usr/bin/env bash
set -euo pipefail
# Modular updater for Apple AirPlay 1 (shairport-sync)
apt-get update && apt-get install -y shairport-sync
systemctl disable --now shairport-sync || true
