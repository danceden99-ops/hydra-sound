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
  - **Spotify Connect** (`go-librespot` primary with `librespot` fallback) — Zeroconf or username/password auth, Now Playing album art, and bidirectional Spotify app volume sync.
  - **Qobuz Connect** (`pibuz`) — Native LAN discovery & Hi-Res FLAC up to 24-bit/192 kHz via direct ALSA backend.
  - **Apple AirPlay 1** (`shairport-sync`) — Lossless 16-bit/44.1 kHz ALAC.
  - **DLNA / UPnP** (`gmrender-resurrect`) — Bit-perfect Hi-Res PCM/FLAC.
  - **Google Cast** (`shanocast`) — *Optional, disabled by default in Web GUI.*
- **USB DAC Hotplug & Stable Identity (`pyudev`):**
  - Matches USB DACs by USB serial number and ALSA card ID rather than fragile numeric `hw:X,0` indices across reboots or replugs.
  - Automatically mutes and stops renderers on USB DAC unplug, and safely re-arms speaker protection + restores the pipeline on replug.
- **Configurable Stream Lock & Soft Release:**
  - **Stream Lock Policy** (configurable in the **Renderers** tab):
    - `Preempt When Paused` *(Default)* — Protects active playback, while allowing another renderer to take over immediately as soon as the current stream is paused.
    - `Last-Wins (Auto-Switch)` — Pressing Play on any renderer immediately takes over the output.
    - `Strict (First-Come-First-Serve)` — Holds the lock until Stop/Disconnect or Pause Timeout expires.
  - **Soft Release:** When the configurable **Pause Timeout** (`5s`–`3600s`, default `60s`) expires, Hydra Sound releases the logical stream lock without restarting the renderer process, keeping Spotify/Qobuz Connect sessions alive on your phone/tablet.
- **Flexible Volume Control & Client Sync:**
  - **Disabled:** 0 dB pass-through for downstream preamps/DACs/AVRs (with USB DAC speaker-protection mute until confirmed).
  - **Hardware:** Controls the ALSA hardware mixer (`amixer -M`) on USB DACs/HATs (e.g. XMOS UAC2), with live warning banner if no usable mixer exists.
  - **Software (32-bit+):** Real-time CamillaDSP WebSocket `Aux1` fader gain (`SetFaderVolume` / `SetFaderMute`).
  - Configurable **Logarithmic** or **Linear** curve, **Power-On Volume**, **Max Volume Limit**, and **Step Size**, with bidirectional volume sync to `go-librespot`.
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

## Volume Safety & Max-Limit Protections

Hydra Sound is designed to **fail closed** so a misconfiguration, reboot, or hotplug event never sends unexpected full-scale audio to your speakers:

- **Single Central Clamp Under Async Lock:** Every volume mutation (`apply_power_on`, `set_volume`, `step_volume`, `apply`, and Web GUI `PUT /api/config/volume`) executes under `VolumeController._lock` (`asyncio.Lock`), enforcing `0..max_volume` consistently across Web GUI, IR remote, and Spotify Connect client sync while preserving mute state across settings saves.
- **Independent Environment Hard Cap:** Setting `HYDRA_HARD_MAX_VOLUME` (0–100) in the service environment enforces an un-overridable ceiling above the Web GUI configuration, reflected in both `VolumeController` and CamillaDSP's YAML `limit`.
- **Safe Power-On & Hotplug Reset:** On boot, device change, or USB DAC replug, volume resets to the clamped `power_on_volume` rather than restoring a potentially loud previous session, and hardware mixers are attenuated *before* renderers start.
- **Fail-Closed CamillaDSP Startup, Rate-Change Re-apply & YAML Ceiling:** In Software Volume mode, CamillaDSP launches with `--gain=-80.0 --mute --gain1=-80.0 --mute1` (muting both `Main` and `Aux1`) and only unmutes once the WebSocket connection applies the clamped level. The `Aux1` `Volume` filter embeds `limit` (in dB) directly in YAML using the unified `percent_to_db()` curve, reloads automatically if `max_volume` or `curve` changes, and re-applies volume immediately after any sample-rate restart.
- **Sysfs/Procfs USB DAC Speaker Protection (Disabled Mode):** USB DACs are detected via `/proc/asound/cardX/usbid` and `/sys/class/sound/cardX/device` (even if the model name omits `"usb"`), engaging a `-80.0 dB` muted CamillaDSP `safety_mute` stage in Disabled mode until you explicitly click **I Confirm — Unmute 0 dB Pass-Through** in the Web GUI. This confirmation automatically re-arms if you change devices or unplug/replug the USB DAC.
- **Hardware Mixer Capability Validation & dB Scaling:** In Hardware mode, non-volume controls (such as `clock`, `source`, `sync`, or `mic` controls) are filtered out and `amixer sget` verifies `pvolume` capability. When the mixer reports a dB scale, Hydra Sound applies explicit `dB` values matching the selected `logarithmic`/`linear` curve; if a device has no usable playback mixer (e.g. HDMI) or `amixer` fails, an explicit warning banner is surfaced in the Web GUI instead of silently failing open.

---

## Audio Quality & Signal Purity Architecture

Every stage of the Hydra Sound pipeline is engineered to preserve bit-perfect integrity when bypass is possible and maximize precision when DSP is required:

- **Auto Bit-Perfect Bypass (Default):** When Resampling is off and Volume Mode is `Disabled` (confirmed) or `Hardware`, CamillaDSP and the ALSA Loopback are completely bypassed—renderers open the DAC directly (`hw:X,Y`, or `hdmi:CARD=X,DEV=Y` with IEC958 framing for Raspberry Pi HDMI across `go-librespot`, `librespot`, `pibuz`, `shairport-sync`, and `gmediarender`).
- **64-Bit Float Internal Precision:** When Software Volume or Resampling is active, audio routes into CamillaDSP's `float64` engine (whose 53-bit mantissa holds 32-bit integer PCM losslessly at 0 dB).
- **Dynamic Native-Rate DSP (`alsa_cdsp`) with Loopback Fallback:** When `alsa_cdsp` (`libasound_module_pcm_cdsp.so`) is installed, renderers open `pcm.camilladsp` directly so Qobuz (44.1 / 96 / 192 kHz), DLNA, Spotify, and AirPlay all enter CamillaDSP at their **exact native sample rate** with zero intermediate ALSA resampling—while `camilladsp_state.yml` + WebSocket `SetFaderVolume` + YAML `limit` preserve full max-volume enforcement across every stream start and rate change. If `alsa_cdsp` is absent, Hydra Sound falls back to `snd-aloop` with `speexrate_best`.
- **DAC-Aware Playback Format Selection:** Without resampling, Hydra Sound inspects only the `Playback:` section of `/proc/asound/cardX/stream0` (mapping `S24_3LE` → `S24_3_LE` and `S24_LE` → `S24_4_RJ_LE`) and `hw_params` to select the highest supported format (`S32_LE` > `S24_3_LE` > `S24_4_RJ_LE` > `S16_LE`) so 32-bit DACs receive native `S32_LE` while 16/24-bit DACs never truncate or fail.
- **Oversampling Headroom (`Limiter`) & `Dither`:** When resampling is enabled, CamillaDSP inserts a `-0.1 dB` `Limiter` to prevent inter-sample clipping; whenever Software Volume or Resampling targets 16-bit or 24-bit output, `Flat` TPDF `Dither` is applied before integer truncation, paired with `enable_rate_adjust: true` for smooth clock-drift tracking.
- **Bit-Transparent Renderer Settings:** `go-librespot` runs with `external_volume: true` (zero digital sample attenuation inside Spotify), `librespot` fallback uses `select_best_pcm_format()` to pass `--format S32 --dither none` or `--format S24_3 --dither none --volume-ctrl fixed` on >16-bit DACs, `shairport-sync` uses `ignore_volume_control = "yes"` and `output_rate = "auto"`, and `gmediarender` initializes at `0 dB`.
- **Fail-Closed Binary Verification:** Release tarballs and module updates verify `.sha256` checksums before extracting binaries onto the Pi.

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

- **[go-librespot](https://github.com/devgianlu/go-librespot)** (`devgianlu/go-librespot`) — Spotify Connect daemon with REST/WebSocket metadata, album artwork, and bidirectional volume sync.
- **[librespot](https://github.com/librespot-org/librespot)** (`librespot-org/librespot`) — Open-source Spotify Connect client library (fallback).
- **[pibuz](https://github.com/PhilipVinc/pibuz)** (`PhilipVinc/pibuz`) — Bit-perfect Qobuz Connect headless receiver.
- **[shairport-sync](https://github.com/mikebrady/shairport-sync)** (`mikebrady/shairport-sync`) — High-precision Apple AirPlay audio player.
- **[gmrender-resurrect](https://github.com/hzeller/gmrender-resurrect)** (`hzeller/gmrender-resurrect`) — Lightweight DLNA / UPnP media renderer.
- **[shanocast](https://github.com/rgerganov/shanocast)** (`rgerganov/shanocast`) — Open-source Google Cast receiver implementation.
- **[CamillaDSP](https://github.com/HEnquist/camilladsp)** (`HEnquist/camilladsp`) — High-precision 64-bit floating-point DSP and resampling engine.

### Supporting Libraries & Tooling

- **[pyudev](https://github.com/pyudev/pyudev)** — Linux `libudev` bindings powering USB DAC hotplug detection and stable serial/card-ID identity.
- **[FastAPI](https://fastapi.tiangolo.com/)**, **[Uvicorn](https://www.uvicorn.org/)**, & **[Pydantic](https://docs.pydantic.dev/)** — Async REST/WebSocket API, static Web GUI serving, and typed configuration validation.
- **[websockets](https://websockets.readthedocs.io/)**, **[httpx](https://www.python-httpx.org/)**, **[PyYAML](https://pyyaml.org/)**, & **[psutil](https://github.com/giampaolo/psutil)** — CamillaDSP/`go-librespot` WebSocket control, GitHub release/update queries, YAML config generation, and system telemetry.
- **[alsa-plugins](https://github.com/alsa-project/alsa-plugins)** (`speexrate_best`) — High-quality ALSA rate conversion fallback when ALSA `plug` is engaged.
- **[Ruff](https://docs.astral.sh/ruff/)**, **[ShellCheck](https://www.shellcheck.net/)**, & **[pytest](https://docs.pytest.org/)** — Python/shell linting and automated unit testing in CI.

Thank you to everyone who dedicates their time to building and maintaining these open-source tools for the audiophile and Linux communities.

---

## License

Released under the [MIT License](LICENSE).
