"""Unified Volume Controller: Disabled, Hardware ALSA, and 32-bit+ Software."""

from __future__ import annotations

import asyncio
import logging
import subprocess
from typing import Callable, Optional

from app.audio.alsa import is_usb_audio_device, list_mixers
from app.audio.camilladsp import (
    CamillaDSPManager,
    get_effective_max_volume,
    percent_to_db,
)
from app.config import AudioOutputConfig, VolumeConfig

logger = logging.getLogger(__name__)


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
        self.hardware_error: Optional[str] = None

    def clamp_volume(self, value: int, cfg: VolumeConfig) -> int:
        max_allowed = get_effective_max_volume(cfg)
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
            self.hardware_error = None
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
            self.hardware_error = None
            max_allowed = get_effective_max_volume(vol_cfg)
            max_db = percent_to_db(max_allowed, vol_cfg.curve)
            db = min(percent_to_db(vol_cfg.current_volume, vol_cfg.curve), max_db)
            await self.camilla.set_volume_db(db, muted=vol_cfg.muted)

        if notify_client and self.on_volume_changed:
            self.on_volume_changed(vol_cfg.current_volume, vol_cfg.muted)

    def _mixer_info(self, card: str, mixer: str) -> tuple[bool, bool]:
        """Return (has_pvolume, supports_db) for an ALSA simple mixer control."""
        try:
            res = subprocess.run(
                ["amixer", "-c", card, "sget", mixer],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if res.returncode != 0:
                return False, False
            out = res.stdout or ""
            has_pvol = "pvolume" in out.lower() or "playback channels:" in out.lower()
            has_db = "db" in out.lower()
            return has_pvol, has_db
        except Exception:
            return False, False

    def _apply_hardware_alsa(self, device_id: str, vol_cfg: VolumeConfig) -> None:
        card = "0"
        if ":" in device_id:
            card = device_id.split(":", 1)[1].split(",", 1)[0]
        mixer = vol_cfg.hardware_mixer_name or "PCM"
        supports_db = False
        try:
            available = list_mixers(int(card))
            non_vol_keywords = (
                "mic",
                "capture",
                "adc",
                "input",
                "boost",
                "clock",
                "source",
                "sync",
                "internal",
                "valid",
            )
            playback_mixers = []
            for m in available:
                if any(k in m.lower() for k in non_vol_keywords):
                    continue
                has_pvol, _ = self._mixer_info(card, m)
                if has_pvol:
                    playback_mixers.append(m)

            if mixer in available:
                has_pvol, supports_db = self._mixer_info(card, mixer)
                if not has_pvol and playback_mixers:
                    mixer = playback_mixers[0]
                    _, supports_db = self._mixer_info(card, mixer)
                elif not has_pvol:
                    self.hardware_error = (
                        f"Mixer '{mixer}' on {device_id} has no playback volume capability."
                    )
                    logger.warning(self.hardware_error)
                    return
            elif playback_mixers:
                logger.info(
                    "ALSA mixer '%s' not found on card %s; using '%s'.",
                    mixer,
                    card,
                    playback_mixers[0],
                )
                mixer = playback_mixers[0]
                _, supports_db = self._mixer_info(card, mixer)
            else:
                self.hardware_error = (
                    f"No usable hardware mixer on {device_id}; hardware volume limit is NOT active."
                )
                logger.warning(self.hardware_error)
                return
        except Exception as exc:
            self.hardware_error = f"Unable to query ALSA mixers on {device_id}: {exc}"
            logger.warning(self.hardware_error)
            return
        safe_level = self.clamp_volume(vol_cfg.current_volume, vol_cfg)
        if vol_cfg.muted:
            target = "0%"
        elif supports_db:
            db_val = percent_to_db(safe_level, vol_cfg.curve)
            target = f"{db_val:.2f}dB"
        else:
            target = f"{safe_level}%"
        try:
            cmd = (
                ["amixer", "-c", card, "sset", mixer, "--", target]
                if supports_db and not vol_cfg.muted
                else ["amixer", "-M", "-c", card, "sset", mixer, target]
            )
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=2,
            )
            if res.returncode != 0:
                err = (res.stderr or res.stdout).strip()
                self.hardware_error = (
                    f"ALSA hardware mixer '{mixer}' failed on card {card}: {err}"
                )
                logger.warning(self.hardware_error)
            else:
                self.hardware_error = None
        except Exception as exc:
            self.hardware_error = f"ALSA hardware mixer update failed: {exc}"
            logger.warning(self.hardware_error)
