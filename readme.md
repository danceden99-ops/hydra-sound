# Hydra Sound

**Headless, Bit-Perfect Audiophile Network Audio Renderer for Raspberry Pi 4 & 5**

Hydra Sound turns a Raspberry Pi 4 or 5 into a discoverable, high-resolution LAN audio renderer with a modern Web GUI, **Dynamic Bypass** bit-perfect ALSA output, optional 64-bit float DSP (32-bit+ software volume & Sinc/SoX resampling), IR remote control (FLIRC USB & GPIO), and modular streaming protocol adapters.

> **Primary Goal:** Highest possible sound quality via bit-perfect ALSA hardware pass-through (`hw:X,Y`) by default.

---

## Features

- **Audio Output Selection:** Selectable via Web GUI between **USB DAC**, **HDMI**, or built-in audio interfaces (note: Raspberry Pi 5 has HDMI and USB/HAT only).
- **Dynamic Bypass Audio Pipeline:**
  - **Bit-Perfect Mode (Default):** When Resampling is off and Volume is set to *Disabled* or *Hardware*, audio streams bypass all mixing and DSP directly to ALSA `hw:X,Y`, matching the source bit depth, sample rate, and channel count bit-for-bit.
  - **DSP Mode (Optional):** When *Software Volume* or *Resampling* is enabled, streams route through `snd-aloop` → **CamillaDSP** (64-bit floating-point internal processing) with selectable target sample rate (44.1 kHz – 384 kHz), bit depth (16/24/32-bit), and resampling algorithm (`SincVeryHigh`, `SincHigh`, `SincMedium`, `SincFast`).
- **Supported Streaming Protocols (First-Come-First-Serve):**
  - **Spotify Connect** (`librespot`) — Zeroconf or username/password auth.
  - **Qobuz Connect** (`pibuz`) — Native LAN discovery & Hi-Res FLAC up to 24-bit/192 kHz.
  - **Apple AirPlay 1** (`shairport-sync`) — Lossless 16-bit/44.1 kHz ALAC.
  - **DLNA / UPnP** (`gmrender-resurrect`) — Bit-perfect Hi-Res PCM/FLAC.
  - **Google Cast** (`shanocast`) — *Optional, disabled by default in Web GUI.*
- **First-Come-First-Serve Stream Orchestrator:**
  - Locks audio output to the active streaming client.
  - Releases the lock immediately when the user hits **Stop** (or disconnects), or automatically after a configurable **Pause Timeout** (default: `60s`).
- **Flexible Volume Control & Client Sync:**
  - **Disabled:** 0 dB pass-through for downstream preamps/DACs.
  - **Hardware:** Controls the ALSA hardware mixer (`amixer`) on the DAC/HDMI.
  - **Software (32-bit+):** High-precision CamillaDSP master gain.
  - Configurable **Logarithmic** or **Linear** curve, **Power-On Volume**, **Max Volume Limit**, and **Step Size**, with volume feedback pushed back to active client apps.
- **IR Remote Control:**
  - **FLIRC USB** with interactive Web GUI IR learning (Volume+, Volume-, Mute, Clear).
  - **GPIO IR Receiver** with Web GUI-selectable BCM GPIO pin (`dtoverlay=gpio-ir`).
- **Live Telemetry & Web GUI:**
  - Real-time WebSocket status for Now Playing metadata, incoming vs. outgoing bit depth/sample rate/format, CPU load, and Raspberry Pi temperature.
  - Zero-config mDNS access at `http://hydrasound.local:8000` with Web GUI **DHCP / Static IP** configuration.
- **Modular Upstream Update Notifier:**
  - Each protocol adapter is isolated in `backend/app/renderers/` and `scripts/modules/`.
  - The Web GUI checks GitHub Releases for upstream updates and lets you trigger per-module updates.

---

## Open-Source Audio Quality & Protocol Limitations

Because Hydra Sound relies on open-source protocol implementations, please note these prerequisites and limitations:

| Service | Module | Prerequisites | Audio Quality & Limitations |
| :--- | :--- | :--- | :--- |
| **Spotify Connect** | `librespot` | Spotify Premium | Capped at **320 kbps Ogg Vorbis**. Spotify's Lossless FLAC tier uses proprietary PlayPlay DRM that open-source clients cannot decrypt. |
| **Qobuz Connect** | `pibuz` | Qobuz subscription | Supports **bit-perfect Hi-Res FLAC up to 24-bit / 192 kHz**. Community reverse-engineered; may require module updates if Qobuz changes protocol. |
| **Apple AirPlay** | `shairport-sync` | Apple / AirPlay sender | Configured for **AirPlay 1** for **lossless 16-bit / 44.1 kHz ALAC** (AirPlay 2 often transcodes to 256 kbps AAC). |
| **DLNA / UPnP** | `gmrender-resurrect` | UPnP control point | Open standard; **bit-perfect PCM/FLAC** up to DAC hardware limits. |
| **Google Cast** | `shanocast` | Chrome / Cast sender | **Disabled by default.** Google Cast requires Google-signed hardware DRM certificates; native mobile apps frequently reject unofficial receivers. Best-effort for desktop Chrome tab casting only. |

---

## Quick Start (Raspberry Pi 4 / 5)

1. Flash **Raspberry Pi OS Lite (64-bit)** (Debian Bookworm or newer) and boot your Pi.
2. Clone the repository and run the installer:
   ```bash
   git clone https://github.com/danceden99-ops/hydra-sound.git
   cd hydra-sound
   sudo bash scripts/install.sh
   ```
3. Open the Web GUI from any device on your LAN:
   ```text
   http://hydrasound.local:8000
   ```

For full technical details, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Development & Testing

Run the unit test suite locally (Python 3.11+):

```bash
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r backend/requirements.txt
pytest backend/tests -v
```

---

## License

Released under the [MIT License](LICENSE).
