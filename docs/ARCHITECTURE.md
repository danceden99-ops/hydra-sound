# Hydra Sound — System Architecture & Design

## 1. Design Philosophy
Hydra Sound is a headless, audiophile-grade network audio renderer for **Raspberry Pi 4 and 5** (64-bit Raspberry Pi OS Lite). Its primary objective is **bit-perfect, highest-quality audio reproduction** with modular, independently updatable streaming protocol adapters.

---

## 2. Dynamic Bypass Audio Pipeline

To guarantee bit-perfect playback by default while still supporting 32-bit+ software volume and high-precision resampling when requested, Hydra Sound uses a **Dynamic Bypass** architecture:

```
                         ┌─────────────────────────────────────────┐
  Active Renderer        │  Bit-Perfect Mode                       │
  (librespot, pibuz,     │  (Resampling=OFF & Volume=Disabled/HW)  │──► ALSA hw:X,Y (Direct DAC/HDMI)
   shairport-sync,       └─────────────────────────────────────────┘
   gmrender, shanocast)
                         ┌─────────────────────────────────────────┐
                         │  DSP Mode                               │
                         │  (Software Volume OR Resampling=ON)     │──► ALSA Loopback (hw:Loopback,0,0)
                         └─────────────────────────────────────────┘         │
                                                                             ▼
                                                                     CamillaDSP (64-bit float internal)
                                                                     - 32-bit+ Precision Volume
                                                                     - Sinc / SoX Resampler
                                                                             │
                                                                             ▼
                                                                     ALSA hw:X,Y (Direct DAC/HDMI)
```

- **Bit-Perfect Mode:** Renderers write directly to the selected ALSA hardware device (`hw:X,Y`) with zero mixing, zero resampling, and zero software gain. Sample rate, bit depth, and channel count match the source stream bit-for-bit.
- **DSP Mode:** When **Software Volume** or **Resampling** is enabled in the Web GUI, Hydra Sound automatically routes the active renderer to `hw:Loopback,0,0` (via kernel `snd-aloop`) and launches **CamillaDSP** to process audio in 64-bit floating-point precision before outputting to `hw:X,Y` at the target bit depth (e.g., `S32_LE`, `S24_LE`).

---

## 3. First-Come-First-Serve Stream Orchestrator

Only one streaming protocol may own the audio output at a time:

1. **IDLE:** All enabled renderers advertise on the LAN (`Avahi` / mDNS).
2. **ACTIVE (Playing):** When a renderer receives a stream, it notifies the Orchestrator (`/api/internal/event`). The Orchestrator locks the audio output to that renderer. Incoming streams from other renderers are rejected/paused until the lock is released.
3. **PAUSED (Timeout):** If the active client pauses playback, a configurable **Pause Timeout** timer starts (e.g., 60 seconds).
   - If the client resumes before timeout -> returns to **ACTIVE**.
   - If the client hits **Stop**, disconnects, or the pause timer expires -> the active renderer's playback is stopped cleanly and the lock returns to **IDLE**.

---

## 4. Volume Control & Bidirectional Client Sync

Three selectable volume modes:
1. **Disabled:** Full-scale 0 dB output; volume handled by downstream pre-amp/DAC/AVR.
2. **Hardware:** Adjusts the ALSA hardware mixer (`amixer`) on the USB DAC or HDMI interface.
3. **Software (32-bit+):** Adjusts CamillaDSP master gain in 64-bit float / 32-bit integer output precision.

Features:
- **Curves:** Selectable **Logarithmic** (perceptually linear dB scale) or **Linear** mapping.
- **Limits & Steps:** Configurable Power-On Volume, Maximum Volume Limit, and Step Size (%).
- **Client App Sync:** When volume changes via Web GUI or IR remote, the Orchestrator pushes the updated volume level back to the active renderer (`librespot`, `pibuz`, `shairport-sync`, `gmrender`) so the native app's volume slider stays synchronized.

---

## 5. Open-Source Limitations & Prerequisites

| Service | Open-Source Module | Prerequisites | Audio Quality & Limitations |
| :--- | :--- | :--- | :--- |
| **Spotify Connect** | `librespot` | Spotify Premium account | Capped at **320 kbps Ogg Vorbis**. Spotify Lossless FLAC uses proprietary PlayPlay DRM that open-source clients cannot decrypt. |
| **Qobuz Connect** | `pibuz` | Qobuz subscription | Supports **bit-perfect Hi-Res FLAC up to 24-bit / 192 kHz**. Community reverse-engineered; may require module updates if Qobuz changes protocol. |
| **Apple AirPlay** | `shairport-sync` | Apple device / AirPlay sender | Configured for **AirPlay 1** by default for **lossless 16-bit / 44.1 kHz ALAC**. (AirPlay 2 often transcodes to 256 kbps AAC). |
| **DLNA / UPnP** | `gmrender-resurrect` | Local media server / control point | Open standard; **bit-perfect PCM/FLAC** up to DAC hardware limits. |
| **Google Cast** | `shanocast` *(Optional, Disabled by default)* | Chrome / Cast sender | Proprietary Google certificate DRM blocks most native mobile apps (YouTube Music, Android Cast); best-effort for Chrome tab casting only. |

---

## 6. Modularity & Upstream Updates
Each streaming renderer is wrapped by an independent adapter in `backend/app/renderers/` and paired with an isolated install/update script in `scripts/modules/`. The Web GUI periodically checks GitHub Releases for newer versions of `librespot`, `pibuz`, `shairport-sync`, `gmrender-resurrect`, `shanocast`, and `camilladsp`, surfacing update notifications and one-click update triggers.
