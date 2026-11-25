#!/usr/bin/env bash
set -euo pipefail

# Hydra Sound — Raspberry Pi OS Lite (64-bit) Installer
# Installs core dependencies, ALSA loopback, Avahi (hydrasound.local), CamillaDSP, and systemd service.

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root: sudo bash scripts/install.sh"
  exit 1
fi

APP_DIR="/opt/hydrasound"
mkdir -p "$APP_DIR" /etc/hydrasound

apt-get update
apt-get install -y \
  python3 python3-venv python3-pip \
  alsa-utils libasound2-dev avahi-daemon libnss-mdns network-manager \
  shairport-sync gmediarender gstreamer1.0-alsa gstreamer1.0-plugins-good \
  curl git build-essential pkg-config cargo rustc

# Let Hydra Sound orchestrator manage shairport-sync & gmediarender
systemctl disable --now shairport-sync gmediarender || true

# Enable ALSA Loopback for Dynamic DSP Mode
if ! grep -q "^snd-aloop" /etc/modules; then
  echo "snd-aloop" >> /etc/modules
fi
modprobe snd-aloop || true

# Set mDNS hostname to hydrasound.local and restart Avahi so it broadcasts immediately
hostnamectl set-hostname hydrasound
if grep -q "^127\.0\.1\.1" /etc/hosts; then
  sed -i "s/^127\.0\.1\.1.*/127.0.1.1\thydrasound/" /etc/hosts
else
  echo -e "127.0.1.1\thydrasound" >> /etc/hosts
fi
systemctl enable avahi-daemon
systemctl restart avahi-daemon

# Copy application files (including .git for Web GUI release updates)
cp -r backend frontend scripts systemd "$APP_DIR/"
if [[ -d .git ]]; then
  cp -r .git "$APP_DIR/"
fi
chmod +x "$APP_DIR"/scripts/*.sh "$APP_DIR"/scripts/modules/*.sh "$APP_DIR"/scripts/hooks/*.sh

# Python virtualenv
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/backend/requirements.txt"

# Install FLIRC utility (optional USB IR)
curl -sSL https://apt.flirc.tv/install.sh | bash || true

# Install CamillaDSP (ARM64) if not already installed
if ! command -v camilladsp >/dev/null 2>&1; then
  CAMILLA_URL=$(curl -sSL https://api.github.com/repos/HEnquist/camilladsp/releases/latest \
    | grep -o "https://[^\"]*linux-aarch64\\.tar\\.gz" | head -n 1 || true)
  if [[ -n "$CAMILLA_URL" ]]; then
    curl -sSL "$CAMILLA_URL" | tar -xz -C /usr/local/bin camilladsp || true
  fi
fi

# Install Spotify Connect (librespot) & Qobuz Connect (pibuz)
bash "$APP_DIR/scripts/modules/update_librespot.sh" || true
bash "$APP_DIR/scripts/modules/update_pibuz.sh" || true

# Install systemd service
cp "$APP_DIR/systemd/hydrasound.service" /etc/systemd/system/hydrasound.service
systemctl daemon-reload
systemctl enable --now hydrasound.service

echo "Hydra Sound installed! Open http://hydrasound.local:8000"
