#!/usr/bin/env bash
set -euo pipefail
# Modular updater for DLNA/UPnP (gmrender-resurrect)
apt-get update && apt-get install -y gmediarender gstreamer1.0-alsa gstreamer1.0-plugins-good gstreamer1.0-plugins-bad
systemctl disable --now gmediarender || true
