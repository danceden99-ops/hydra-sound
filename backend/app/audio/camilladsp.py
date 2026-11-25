"""CamillaDSP configuration generator and process/WebSocket controller."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path
from typing import Optional

import websockets
import yaml

from app.config import AudioOutputConfig, VolumeConfig

logger = logging.getLogger(__name__)

CAMILLA_CONFIG_PATH = Path("/etc/hydrasound/camilladsp.yml")
CAMILLA_WS_PORT = 1234

RESAMPLER_PROFILES = {
    "SincVeryHigh": {"type": "Synchronous", "profile": "VeryAccurate"},
    "SincHigh": {"type": "Synchronous", "profile": "Accurate"},
    "SincMedium": {"type": "Synchronous", "profile": "Balanced"},
    "SincFast": {"type": "Synchronous", "profile": "Fast"},
}


def bit_depth_to_alsa_format(bit_depth: int) -> str:
    return {
        16: "S16_LE",
        24: "S24_3LE",
        32: "S32_LE",
    }.get(bit_depth, "S32_LE")


def build_camilladsp_config(
    audio_cfg: AudioOutputConfig,
    volume_cfg: VolumeConfig,
    capture_device: str = "hw:Loopback,1,0",
) -> dict:
    """Build a 64-bit float internal precision CamillaDSP YAML dictionary."""
    target_format = (
        bit_depth_to_alsa_format(audio_cfg.resample_bit_depth)
        if audio_cfg.resample_enabled
        else "S32_LE"
    )
    target_rate = audio_cfg.resample_rate if audio_cfg.resample_enabled else 44100

    devices: dict = {
        "samplerate": target_rate,
        "chunksize": 2048,
        "capture": {
            "type": "Alsa",
            "channels": 2,
            "device": capture_device,
            "format": "S32_LE",
        },
        "playback": {
            "type": "Alsa",
            "channels": 2,
            "device": audio_cfg.device_id,
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
        devices["capture_samplerate"] = 44100

    filters = {}
    pipeline = []

    if volume_cfg.mode == "software":
        filters["master_volume"] = {
            "type": "Volume",
            "parameters": {
                "ramp_time": 120,
                "fader": "Main",
            },
        }
        pipeline.append({"type": "Filter", "channels": [0, 1], "names": ["master_volume"]})

    return {
        "devices": devices,
        "filters": filters,
        "pipeline": pipeline,
    }


class CamillaDSPManager:
    """Manages the CamillaDSP daemon and real-time 32-bit+ volume updates."""

    def __init__(self, config_path: Optional[Path] = None) -> None:
        self.config_path = config_path or CAMILLA_CONFIG_PATH
        self._proc: Optional[subprocess.Popen] = None
        self.running = False

    def write_config(self, audio_cfg: AudioOutputConfig, volume_cfg: VolumeConfig) -> Path:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        data = build_camilladsp_config(audio_cfg, volume_cfg)
        self.config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return self.config_path

    def start(self, audio_cfg: AudioOutputConfig, volume_cfg: VolumeConfig) -> None:
        self.stop()
        self.write_config(audio_cfg, volume_cfg)
        try:
            self._proc = subprocess.Popen(
                [
                    "camilladsp",
                    "-p",
                    str(CAMILLA_WS_PORT),
                    "-o",
                    "/var/log/hydrasound-camilladsp.log",
                    str(self.config_path),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
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
        """Send live volume/mute update to CamillaDSP via WebSocket."""
        if not self.running:
            return
        uri = f"ws://127.0.0.1:{CAMILLA_WS_PORT}"
        try:
            async with websockets.connect(uri, open_timeout=1.5) as ws:
                await ws.send(json.dumps({"SetVolume": db}))
                await ws.recv()
                await ws.send(json.dumps({"SetMute": muted}))
                await ws.recv()
        except Exception as exc:
            logger.debug("CamillaDSP WebSocket update skipped: %s", exc)
