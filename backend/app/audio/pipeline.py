"""Dynamic Bypass Audio Pipeline Manager.

Routes renderers directly to ALSA `hw:X,Y` for true bit-perfect playback when
resampling is disabled and volume is Disabled or Hardware. Automatically engages
ALSA Loopback -> CamillaDSP -> ALSA `hw:X,Y` when 32-bit Software Volume or
Resampling is enabled.
"""

from __future__ import annotations

import logging

from typing import Optional

from app.audio.alsa import is_usb_audio_device
from app.audio.camilladsp import CamillaDSPManager, has_alsa_cdsp
from app.config import AudioOutputConfig, RoomEQConfig, VolumeConfig

logger = logging.getLogger(__name__)

LOOPBACK_PLAYBACK_DEVICE = "plughw:CARD=Loopback,DEV=0"
ALSA_CDSP_DEVICE = "camilladsp"


class AudioPipelineManager:
    """Coordinates dynamic bypass vs DSP routing."""

    def __init__(self, camilla: CamillaDSPManager) -> None:
        self.camilla = camilla

    @staticmethod
    def requires_usb_safety_mute(
        audio_cfg: AudioOutputConfig, volume_cfg: VolumeConfig
    ) -> bool:
        return bool(
            volume_cfg.mode == "disabled"
            and not volume_cfg.usb_disabled_confirmed
            and is_usb_audio_device(audio_cfg.device_id, audio_cfg.device_name)
        )

    @staticmethod
    def requires_dsp(
        audio_cfg: AudioOutputConfig,
        volume_cfg: VolumeConfig,
        eq_cfg: Optional[RoomEQConfig] = None,
    ) -> bool:
        return bool(
            audio_cfg.resample_enabled
            or volume_cfg.mode == "software"
            or AudioPipelineManager.requires_usb_safety_mute(audio_cfg, volume_cfg)
            or (eq_cfg is not None and eq_cfg.has_active_processing())
        )

    def get_renderer_alsa_device(
        self,
        audio_cfg: AudioOutputConfig,
        volume_cfg: VolumeConfig,
        eq_cfg: Optional[RoomEQConfig] = None,
    ) -> str:
        if self.requires_dsp(audio_cfg, volume_cfg, eq_cfg):
            return ALSA_CDSP_DEVICE if has_alsa_cdsp() else LOOPBACK_PLAYBACK_DEVICE
        return audio_cfg.device_id

    def apply(
        self,
        audio_cfg: AudioOutputConfig,
        volume_cfg: VolumeConfig,
        capture_rate: int = 44100,
        eq_cfg: Optional[RoomEQConfig] = None,
    ) -> str:
        """Apply current audio & volume config; return ALSA device for renderers."""
        if self.requires_dsp(audio_cfg, volume_cfg, eq_cfg):
            if has_alsa_cdsp():
                logger.info(
                    "DSP mode active via alsa_cdsp (software_vol=%s, resample=%s -> %sHz/%sbit, eq=%s)",
                    volume_cfg.mode == "software",
                    audio_cfg.resample_enabled,
                    audio_cfg.resample_rate,
                    audio_cfg.resample_bit_depth,
                    bool(eq_cfg and eq_cfg.has_active_processing()),
                )
                self.camilla.configure_alsa_cdsp(audio_cfg, volume_cfg, eq_cfg=eq_cfg)
                return ALSA_CDSP_DEVICE

            logger.info(
                "DSP mode active via Loopback (software_vol=%s, resample=%s -> %sHz/%sbit, eq=%s, capture=%sHz)",
                volume_cfg.mode == "software",
                audio_cfg.resample_enabled,
                audio_cfg.resample_rate,
                audio_cfg.resample_bit_depth,
                bool(eq_cfg and eq_cfg.has_active_processing()),
                capture_rate,
            )
            self.camilla.start(audio_cfg, volume_cfg, capture_rate=capture_rate, eq_cfg=eq_cfg)
            return LOOPBACK_PLAYBACK_DEVICE

        logger.info("Bit-perfect bypass active -> %s", audio_cfg.device_id)
        self.camilla.stop()
        return audio_cfg.device_id
