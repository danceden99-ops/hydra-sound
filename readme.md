# Hydra Sound

**Headless, Bit-Perfect Audiophile Network Audio Renderer for Raspberry Pi 4 & 5**

Hydra Sound turns a Raspberry Pi 4 or 5 into a discoverable, high-resolution LAN audio renderer with a modern Web GUI, **Dynamic Bypass** bit-perfect ALSA output, optional 64-bit float DSP (32-bit+ software volume & Sinc/SoX resampling), IR remote control (FLIRC USB & GPIO), and modular streaming protocol adapters.

> **Primary Goal:** Highest possible sound quality via bit-perfect ALSA hardware pass-through (`hw:X,Y`) by default.

---

## Features

- **Audio Output Selection & Capabilities:** Selectable via Web GUI between **USB DAC**, **HDMI**, or built-in audio interfaces (note: Raspberry Pi 5 has HDMI and USB/HAT only), with live **Show Device Capabilities** (`hw_params` & ALSA mixer probing).
- **Dynamic Bypass Audio Pipeline:**
  - **Bit-Perfect Mode (Default):** When Resampling is off and Volume is set to *Disabled* or *Hardware*, audio streams bypass all mixing and DSP directly to ALSA `hw:X,Y` (automatically resolving ALSA card IDs and `plughw`/`hdmi` IEC958 framing for Raspberry Pi `vc4-hdmi` outputs), matching the source bit depth, sample rate, and channel count bit-for-bit.
  - **DSP Mode (Optional):** When *Software Volume* or *Resampling* is enabled, streams route through `plughw:CARD=Loopback,DEV=0` → `hw:Loopback,1,0` → **CamillaDSP 4.x** (64-bit floating-point internal processing) with selectable target sample rate (44.1 kHz – 384 kHz), bit depth (16/24/32-bit, `S24_3_LE` for 24-bit), `enable_rate_adjust`, and `AsyncSinc` resampling profiles (`SincVeryHigh`/`Accurate`, `SincHigh`/`Balanced`, `SincMedium`/`Fast`, `SincFast`/`VeryFast`).
- **Supported Streaming Protocols:**
  - **Spotify Connect** (`librespot`, built from `dev` branch) — Zeroconf or username/password auth.
  - **Qobuz Connect** (`pibuz`) — Native LAN discovery & Hi-Res FLAC up to 24-bit/192 kHz via direct ALSA backend.
  - **Apple AirPlay 1** (`shairport-sync`) — Lossless 16-bit/44.1 kHz ALAC.
  - **DLNA / UPnP** (`gmrender-resurrect`) — Bit-perfect Hi-Res PCM/FLAC.
  - **Google Cast** (`shanocast`) — *Optional, disabled by default in Web GUI.*
- **Configurable Stream Lock & Soft Release:**
  - **Stream Lock Policy** (configurable in the **Renderers** tab):
    - `Preempt When Paused` *(Default)* — Protects active playback, while allowing another renderer to take over immediately as soon as the current stream is paused.
    - `Last-Wins (Auto-Switch)` — Pressing Play on any renderer immediately takes over the output.
    - `Strict (First-Come-First-Serve)` — Holds the lock until Stop/Disconnect or Pause Timeout expires.
  - **Soft Release:** When the configurable **Pause Timeout** (`5s`–`3600s`, default `60s`) expires, Hydra Sound releases the logical stream lock without restarting the renderer process, keeping Spotify/Qobuz Connect sessions alive on your phone/tablet.
- **Flexible Volume Control & Client Sync:**
  - **Disabled:** 0 dB pass-through for downstream preamps/DACs/AVRs.
  - **Hardware:** Controls the ALSA hardware mixer (`amixer`) on USB DACs/HATs (e.g. XMOS UAC2).
  - **Software (32-bit+):** Real-time CamillaDSP WebSocket gain (`SetVolume` / `SetMute`).
  - Configurable **Logarithmic** or **Linear** curve, **Power-On Volume**, **Max Volume Limit**, and **Step Size**, with volume feedback pushed back to active client apps.
- **IR Remote Control:**
  - **FLIRC USB** with `flirc_util` status/learning in the Web GUI (Volume+, Volume-, Mute, Clear) and a built-in `/dev/input/event*` HID key listener (150ms debounce) wired directly to `VolumeController`.
  - **GPIO IR Receiver** with Web GUI-selectable BCM GPIO pin (`dtoverlay=gpio-ir`) and `/dev/input/event*` listener.
- **Live Telemetry, Networking & Web GUI:**
  - Real-time WebSocket status for **Now Playing** metadata, live renderer connection badges (`Connected`, `Ready`, `Error`, `Stopped`), incoming vs. outgoing bit depth/sample rate/format, CPU load, and Raspberry Pi temperature.
  - Zero-config mDNS access at `http://hydrasound.local:8000` with live interface/IP summary, **DHCP / Static IP**, and **Wi-Fi** scan/country/connect (`nmcli`).
- **System Updater, Power Control & SD-Safe Logging:**
  - **Web GUI Release & Branch Updater:** Check GitHub Releases (or `main`) and install updates + restart `hydrasound.service` directly from the **System** tab without SSH, plus per-module upstream updates in the **Renderers** tab.
  - **SD-Safe File Logging:** Bounded 500-entry rolling log (`hydrasound.log`) with View, Download, and Clear controls — **Off by default** to prevent SD card wear, toggleable live in the **System** tab.
  - Safe **Reboot** and **Shutdown** buttons for headless operation.

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

1. Flash **Raspberry Pi OS Lite (64-bit)** (Debian Bookworm or newer) and boot your Pi. *(Use the **Lite** image rather than Desktop so PipeWire/PulseAudio does not compete for exclusive bit-perfect ALSA access.)*
2. *(Optional)* Enable SSH if you are running headless (either in Raspberry Pi Imager or via `sudo raspi-config` → **Interface Options** → **SSH**).
3. Update Raspberry Pi OS packages to the latest versions and install `git`:
   ```bash
   sudo apt-get update
   sudo apt-get upgrade -y
   sudo apt-get install -y git
   ```
4. Clone the repository and run the installer:
   ```bash
   git clone https://github.com/danceden99-ops/hydra-sound.git
   cd hydra-sound
   sudo bash scripts/install.sh
   ```
   > **Prebuilt ARM64 Binaries:** `install.sh` installs system audio packages (`shairport-sync`, `gmediarender`, `camilladsp`) and downloads prebuilt ARM64 binaries for **Spotify Connect (`librespot`)** and **Qobuz Connect (`pibuz`)** from GitHub Releases (compiled in the cloud via GitHub Actions), so setup typically completes in **~1–2 minutes** without compiling Rust on the Raspberry Pi.
5. Open the Web GUI from any device on your LAN:
   ```text
   http://hydrasound.local:8000
   ```

For full technical details, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Security & Credentials

> **Recommended:** Use **Zeroconf / LAN Discovery** (enabled by default) for **Spotify Connect** and **Qobuz Connect** instead of entering your username and password in the Web GUI. With Zeroconf, your phone or desktop app authenticates directly with the service and streams to Hydra Sound over your local network—so your account credentials are **never stored** on the Raspberry Pi.

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

## Acknowledgments & Open-Source Credits

Hydra Sound would not be possible without the extraordinary work of the open-source audio community. We extend our sincere gratitude to the authors, maintainers, and contributors of the core streaming and DSP projects that power Hydra Sound:

- **[librespot](https://github.com/librespot-org/librespot)** (`librespot-org/librespot`) — Open-source Spotify Connect client library.
- **[pibuz](https://github.com/PhilipVinc/pibuz)** (`PhilipVinc/pibuz`) — Bit-perfect Qobuz Connect headless receiver.
- **[shairport-sync](https://github.com/mikebrady/shairport-sync)** (`mikebrady/shairport-sync`) — High-precision Apple AirPlay audio player.
- **[gmrender-resurrect](https://github.com/hzeller/gmrender-resurrect)** (`hzeller/gmrender-resurrect`) — Lightweight DLNA / UPnP media renderer.
- **[shanocast](https://github.com/rgerganov/shanocast)** (`rgerganov/shanocast`) — Open-source Google Cast receiver implementation.
- **[CamillaDSP](https://github.com/HEnquist/camilladsp)** (`HEnquist/camilladsp`) — High-precision 64-bit floating-point DSP and resampling engine.

Thank you to everyone who dedicates their time to building and maintaining these open-source tools for the audiophile and Linux communities.

---

## License

Released under the [MIT License](LICENSE).
