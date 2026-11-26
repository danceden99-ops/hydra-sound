"""CamillaDSP configuration generator and process/WebSocket controller."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

import websockets
import yaml

from app.audio.alsa import select_best_pcm_format
from app.config import AudioOutputConfig, VolumeConfig

logger = logging.getLogger(__name__)

def _default_camilla_config_path() -> Path:
    cfg_env = os.environ.get("HYDRA_CONFIG_PATH")
    if cfg_env:
        return Path(cfg_env).parent / "camilladsp.yml"
    etc_dir = Path("/etc/hydrasound")
    if etc_dir.exists() and os.access(etc_dir, os.W_OK):
        return etc_dir / "camilladsp.yml"
    return Path(__file__).resolve().parents[2] / "data" / "camilladsp.yml"


CAMILLA_WS_PORT = 1234

RESAMPLER_PROFILES = {
    "SincVeryHigh": {"type": "AsyncSinc", "profile": "Accurate"},
    "SincHigh": {"type": "AsyncSinc", "profile": "Balanced"},
    "SincMedium": {"type": "AsyncSinc", "profile": "Fast"},
    "SincFast": {"type": "AsyncSinc", "profile": "VeryFast"},
}


def bit_depth_to_alsa_format(bit_depth: int) -> str:
    return {
        16: "S16_LE",
        24: "S24_3_LE",
        32: "S32_LE",
    }.get(bit_depth, "S32_LE")


def percent_to_db(percent: int, curve: str = "logarithmic", min_db: float = -80.0) -> float:
    """Convert 0..100% volume into dB attenuation (-80.0..0.0 dB)."""
    pct = max(0, min(100, int(percent)))
    if pct == 0:
        return min_db
    if pct == 100:
        return 0.0
    if curve == "linear":
        import math

        return max(min_db, 20.0 * math.log10(pct / 100.0))
    # Logarithmic (perceptual audio taper: 50% ~ -20.8 dB, 1% ~ -77.1 dB)
    norm = pct / 100.0
    return max(min_db, min_db * ((1.0 - norm) ** 1.95))


def build_camilladsp_config(
    audio_cfg: AudioOutputConfig,
    volume_cfg: VolumeConfig,
    capture_device: str = "hw:Loopback,1,0",
    capture_rate: int = 44100,
) -> dict:
    """Build a 64-bit float internal precision CamillaDSP YAML dictionary."""
    target_format = (
        bit_depth_to_alsa_format(audio_cfg.resample_bit_depth)
        if audio_cfg.resample_enabled
        else select_best_pcm_format(audio_cfg.device_id)
    )
    target_rate = audio_cfg.resample_rate if audio_cfg.resample_enabled else capture_rate

    playback_device = audio_cfg.device_id
    if (
        "hdmi" in audio_cfg.device_name.lower()
        and playback_device.startswith("hw:")
        and not playback_device.startswith("hw:Loopback")
    ):
        card_dev = playback_device.split(":", 1)[1].split(",")
        if len(card_dev) == 2:
            playback_device = f"hdmi:CARD={card_dev[0]},DEV={card_dev[1]}"

    chunksize = 1024 if target_rate <= 48000 else (2048 if target_rate <= 96000 else 4096)

    devices: dict = {
        "samplerate": target_rate,
        "chunksize": chunksize,
        "enable_rate_adjust": True,
        "capture": {
            "type": "Alsa",
            "channels": 2,
            "device": capture_device,
            "format": "S32_LE",
        },
        "playback": {
            "type": "Alsa",
            "channels": 2,
            "device": playback_device,
            "format": target_format,
        },
    }

    if audio_cfg.resample_enabled:
        profile = RESAMPLER_PROFILES.get(
            audio_cfg.resample_algorithm, RESAMPLER_PROFILES["SincVeryHigh"]
        )
        devices["resampler"] = {
            "type": "AsyncSinc",
            "profile": profile["profile"],
        }
        devices["capture_samplerate"] = capture_rate

    filters = {}
    pipeline = []

    if volume_cfg.mode == "disabled" and not volume_cfg.usb_disabled_confirmed:
        filters["safety_mute"] = {
            "type": "Gain",
            "parameters": {
                "gain": -80.0,
                "mute": True,
            },
        }
        pipeline.append({"type": "Filter", "channels": [0, 1], "names": ["safety_mute"]})
    elif volume_cfg.mode == "software":
        max_db = round(percent_to_db(volume_cfg.max_volume, volume_cfg.curve), 2)
        filters["master_volume"] = {
            "type": "Volume",
            "parameters": {
                "ramp_time": 120,
                "fader": "Aux1",
                "limit": max_db,
            },
        }
        pipeline.append({"type": "Filter", "channels": [0, 1], "names": ["master_volume"]})

    if audio_cfg.resample_enabled:
        filters["dsp_limiter"] = {
            "type": "Limiter",
            "parameters": {
                "clip_limit": -0.1,
                "soft_clip": False,
            },
        }
        pipeline.append({"type": "Filter", "channels": [0, 1], "names": ["dsp_limiter"]})
        if audio_cfg.resample_bit_depth in (16, 24):
            filters["dsp_dither"] = {
                "type": "Dither",
                "parameters": {
                    "type": "Flat",
                    "bits": audio_cfg.resample_bit_depth,
                    "amplitude": 2.0,
                },
            }
            pipeline.append({"type": "Filter", "channels": [0, 1], "names": ["dsp_dither"]})

    return {
        "devices": devices,
        "filters": filters,
        "pipeline": pipeline,
    }


class CamillaDSPManager:
    """Manages the CamillaDSP daemon and real-time 32-bit+ volume updates."""

    def __init__(self, config_path: Optional[Path] = None) -> None:
        self.config_path = config_path or _default_camilla_config_path()
        self._proc: Optional[subprocess.Popen] = None
        self.running = False

    def write_config(
        self,
        audio_cfg: AudioOutputConfig,
        volume_cfg: VolumeConfig,
        capture_rate: int = 44100,
    ) -> Path:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        data = build_camilladsp_config(audio_cfg, volume_cfg, capture_rate=capture_rate)
        self.config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return self.config_path

    def start(
        self,
        audio_cfg: AudioOutputConfig,
        volume_cfg: VolumeConfig,
        capture_rate: int = 44100,
    ) -> None:
        new_yaml = yaml.safe_dump(
            build_camilladsp_config(audio_cfg, volume_cfg, capture_rate=capture_rate),
            sort_keys=False,
        )
        if (
            self.running
            and self._proc
            and self._proc.poll() is None
            and self.config_path.exists()
            and self.config_path.read_text(encoding="utf-8") == new_yaml
        ):
            return

        self.stop()
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(new_yaml, encoding="utf-8")
        cmd = [
            "camilladsp",
            "-p",
            str(CAMILLA_WS_PORT),
            "-o",
            "/var/log/hydrasound-camilladsp.log",
        ]
        if volume_cfg.mode == "software" or (
            volume_cfg.mode == "disabled" and not volume_cfg.usb_disabled_confirmed
        ):
            cmd.append("--gain=-80.0")
            cmd.append("--mute")
        cmd.append(str(self.config_path))
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self.running = True
            logger.info("Started CamillaDSP on %s", audio_cfg.device_id)
        except FileNotFoundError:
            logger.warning("camilladsp binary not found; DSP mode simulated.")
            self.running = False

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        self.running = False

    async def set_volume_db(self, db: float, muted: bool = False) -> None:
        """Send live volume/mute update to CamillaDSP via WebSocket (retrying startup)."""
        if not self.running:
            return
        if self._proc and self._proc.poll() is not None:
            self.running = False
            tail = ""
            if self._proc.stdout:
                try:
                    out = self._proc.stdout.read().strip()
                    if out:
                        tail = " | ".join(out.splitlines()[-5:])
                except Exception:
                    pass
            if not tail:
                log_path = Path("/var/log/hydrasound-camilladsp.log")
                if log_path.is_file():
                    try:
                        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                        err_lines = [ln for ln in lines[-10:] if "ERROR" in ln or "Playback" in ln]
                        tail = " | ".join(err_lines or lines[-3:])
                    except Exception:
                        pass
            logger.warning(
                "CamillaDSP process exited; cannot apply volume %.2f dB%s",
                db,
                f" ({tail})" if tail else "",
            )
            return
        uri = f"ws://127.0.0.1:{CAMILLA_WS_PORT}"
        last_exc: Optional[Exception] = None
        for attempt in range(25):
            if not self.running or (self._proc and self._proc.poll() is not None):
                return
            try:
                async with websockets.connect(uri, open_timeout=1.5) as ws:
                    await ws.send(json.dumps({"SetFaderVolume": [1, db]}))
                    await ws.recv()
                    await ws.send(json.dumps({"SetFaderMute": [1, muted]}))
                    await ws.recv()
                return
            except Exception as exc:
                last_exc = exc
                if attempt < 24:
                    await asyncio.sleep(0.1)
        logger.warning("CamillaDSP WebSocket volume update failed after retries: %s", last_exc)
