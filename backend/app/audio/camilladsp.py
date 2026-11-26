"""CamillaDSP configuration generator and process/WebSocket controller."""

from __future__ import annotations

import asyncio
import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

import websockets
import yaml

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
        devices["capture_samplerate"] = 44100

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
        filters["master_volume"] = {
            "type": "Volume",
            "parameters": {
                "ramp_time": 120,
                "fader": "Aux1",
                "limit": 0.0,
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
        self.config_path = config_path or _default_camilla_config_path()
        self._proc: Optional[subprocess.Popen] = None
        self.running = False

    def write_config(self, audio_cfg: AudioOutputConfig, volume_cfg: VolumeConfig) -> Path:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        data = build_camilladsp_config(audio_cfg, volume_cfg)
        self.config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return self.config_path

    def start(self, audio_cfg: AudioOutputConfig, volume_cfg: VolumeConfig) -> None:
        new_yaml = yaml.safe_dump(
            build_camilladsp_config(audio_cfg, volume_cfg), sort_keys=False
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
            cmd.extend(["--gain", "-80.0", "--mute"])
        cmd.append(str(self.config_path))
        try:
            self._proc = subprocess.Popen(
                cmd,
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
        """Send live volume/mute update to CamillaDSP via WebSocket (retrying startup)."""
        if not self.running:
            return
        if self._proc and self._proc.poll() is not None:
            self.running = False
            logger.warning("CamillaDSP process exited; cannot apply volume %.2f dB", db)
            return
        uri = f"ws://127.0.0.1:{CAMILLA_WS_PORT}"
        last_exc: Optional[Exception] = None
        for attempt in range(25):
            if not self.running or (self._proc and self._proc.poll() is not None):
                return
            try:
                async with websockets.connect(uri, open_timeout=1.5) as ws:
                    await ws.send(json.dumps({"SetVolume": db}))
                    await ws.recv()
                    await ws.send(json.dumps({"SetMute": muted}))
                    await ws.recv()
                return
            except Exception as exc:
                last_exc = exc
                if attempt < 24:
                    await asyncio.sleep(0.1)
        logger.warning("CamillaDSP WebSocket volume update failed after retries: %s", last_exc)
