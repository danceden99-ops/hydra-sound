"""Unified Volume Controller: Disabled, Hardware ALSA, and 32-bit+ Software."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import subprocess
from typing import Callable, Optional

from app.audio.alsa import is_usb_audio_device, list_mixers
from app.audio.camilladsp import CamillaDSPManager
from app.config import AudioOutputConfig, VolumeConfig

logger = logging.getLogger(__name__)


def percent_to_db(percent: int, curve: str = "logarithmic", min_db: float = -80.0) -> float:
    """Convert 0-100% volume to dB using logarithmic (perceptual) or linear scaling."""
    clamped = max(0, min(100, percent))
    if clamped == 0:
        return min_db
    ratio = clamped / 100.0
    if curve == "logarithmic":
        # Perceptually smooth exponential amplitude -> dB
        # 100% -> 0 dB, 50% -> ~-18 dB, 1% -> -80 dB
        return round(min_db * (1.0 - math.log10(1.0 + 9.0 * ratio)), 2)
    # Linear amplitude -> dB: 20 * log10(ratio)
    db = 20.0 * math.log10(ratio)
    return round(max(min_db, db), 2)


class VolumeController:
    """Controls volume across Disabled, Hardware, and Software (CamillaDSP) modes."""

    def __init__(
        self,
        camilla: CamillaDSPManager,
        on_volume_changed: Optional[Callable[[int, bool], None]] = None,
    ) -> None:
        self.camilla = camilla
        self.on_volume_changed = on_volume_changed
        self._lock = asyncio.Lock()

    def clamp_volume(self, value: int, cfg: VolumeConfig) -> int:
        max_allowed = max(0, min(100, int(cfg.max_volume)))
        hard_cap_env = os.environ.get("HYDRA_HARD_MAX_VOLUME")
        if hard_cap_env is not None:
            try:
                max_allowed = min(max_allowed, max(0, min(100, int(hard_cap_env))))
            except ValueError:
                pass
        return max(0, min(max_allowed, int(value)))

    async def apply_power_on(
        self, audio_cfg: AudioOutputConfig, vol_cfg: VolumeConfig
    ) -> VolumeConfig:
        async with self._lock:
            vol_cfg.power_on_volume = self.clamp_volume(vol_cfg.power_on_volume, vol_cfg)
            vol_cfg.current_volume = vol_cfg.power_on_volume
            vol_cfg.muted = False
            await self._apply_locked(audio_cfg, vol_cfg, notify_client=True)
            return vol_cfg

    async def set_volume(
        self,
        level: int,
        audio_cfg: AudioOutputConfig,
        vol_cfg: VolumeConfig,
        notify_client: bool = True,
    ) -> VolumeConfig:
        async with self._lock:
            if vol_cfg.mode == "disabled":
                return vol_cfg
            vol_cfg.current_volume = self.clamp_volume(level, vol_cfg)
            await self._apply_locked(audio_cfg, vol_cfg, notify_client=notify_client)
            return vol_cfg

    async def step_volume(
        self,
        direction: int,
        audio_cfg: AudioOutputConfig,
        vol_cfg: VolumeConfig,
    ) -> VolumeConfig:
        async with self._lock:
            if vol_cfg.mode == "disabled":
                return vol_cfg
            delta = vol_cfg.step_size if direction > 0 else -vol_cfg.step_size
            vol_cfg.current_volume = self.clamp_volume(
                vol_cfg.current_volume + delta, vol_cfg
            )
            await self._apply_locked(audio_cfg, vol_cfg, notify_client=True)
            return vol_cfg

    async def toggle_mute(
        self, audio_cfg: AudioOutputConfig, vol_cfg: VolumeConfig
    ) -> VolumeConfig:
        async with self._lock:
            if vol_cfg.mode == "disabled":
                return vol_cfg
            vol_cfg.muted = not vol_cfg.muted
            await self._apply_locked(audio_cfg, vol_cfg, notify_client=True)
            return vol_cfg

    async def apply(
        self,
        audio_cfg: AudioOutputConfig,
        vol_cfg: VolumeConfig,
        notify_client: bool = True,
    ) -> None:
        async with self._lock:
            await self._apply_locked(audio_cfg, vol_cfg, notify_client=notify_client)

    async def _apply_locked(
        self,
        audio_cfg: AudioOutputConfig,
        vol_cfg: VolumeConfig,
        notify_client: bool = True,
    ) -> None:
        if vol_cfg.mode == "disabled":
            if not vol_cfg.usb_disabled_confirmed and is_usb_audio_device(
                audio_cfg.device_id, audio_cfg.device_name
            ):
                logger.warning(
                    "USB DAC speaker protection active: muting output until Volume=Disabled is confirmed."
                )
                await self.camilla.set_volume_db(-80.0, muted=True)
            return

        vol_cfg.current_volume = self.clamp_volume(vol_cfg.current_volume, vol_cfg)

        if vol_cfg.mode == "hardware":
            self._apply_hardware_alsa(audio_cfg.device_id, vol_cfg)
        elif vol_cfg.mode == "software":
            max_db = percent_to_db(vol_cfg.max_volume, vol_cfg.curve)
            db = min(percent_to_db(vol_cfg.current_volume, vol_cfg.curve), max_db)
            await self.camilla.set_volume_db(db, muted=vol_cfg.muted)

        if notify_client and self.on_volume_changed:
            self.on_volume_changed(vol_cfg.current_volume, vol_cfg.muted)

    def _apply_hardware_alsa(self, device_id: str, vol_cfg: VolumeConfig) -> None:
        card = "0"
        if ":" in device_id:
            card = device_id.split(":", 1)[1].split(",", 1)[0]
        mixer = vol_cfg.hardware_mixer_name or "PCM"
        try:
            available = list_mixers(int(card))
            playback_mixers = [
                m for m in available
                if not any(k in m.lower() for k in ("mic", "capture", "adc", "input", "boost"))
            ] or available
            if playback_mixers and mixer not in available:
                logger.info(
                    "ALSA mixer '%s' not found on card %s; using '%s'.",
                    mixer,
                    card,
                    playback_mixers[0],
                )
                mixer = playback_mixers[0]
        except Exception:
            pass
        safe_level = self.clamp_volume(vol_cfg.current_volume, vol_cfg)
        target = "0%" if vol_cfg.muted else f"{safe_level}%"
        try:
            res = subprocess.run(
                ["amixer", "-M", "-c", card, "sset", mixer, target],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if res.returncode != 0:
                logger.warning(
                    "ALSA hardware mixer '%s' on card %s failed: %s",
                    mixer,
                    card,
                    (res.stderr or res.stdout).strip(),
                )
        except Exception as exc:
            logger.warning("ALSA hardware mixer update failed: %s", exc)
