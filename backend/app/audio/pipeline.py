"""Dynamic Bypass Audio Pipeline Manager.

Routes renderers directly to ALSA `hw:X,Y` for true bit-perfect playback when
resampling is disabled and volume is Disabled or Hardware. Automatically engages
ALSA Loopback -> CamillaDSP -> ALSA `hw:X,Y` when 32-bit Software Volume or
Resampling is enabled.
"""

from __future__ import annotations

import logging

from app.audio.camilladsp import CamillaDSPManager
from app.config import AudioOutputConfig, VolumeConfig

logger = logging.getLogger(__name__)

LOOPBACK_PLAYBACK_DEVICE = "plughw:CARD=Loopback,DEV=0"


class AudioPipelineManager:
    """Coordinates dynamic bypass vs DSP routing."""

    def __init__(self, camilla: CamillaDSPManager) -> None:
        self.camilla = camilla

    @staticmethod
    def requires_dsp(audio_cfg: AudioOutputConfig, volume_cfg: VolumeConfig) -> bool:
        return bool(audio_cfg.resample_enabled or volume_cfg.mode == "software")

    def get_renderer_alsa_device(
        self, audio_cfg: AudioOutputConfig, volume_cfg: VolumeConfig
    ) -> str:
        if self.requires_dsp(audio_cfg, volume_cfg):
            return LOOPBACK_PLAYBACK_DEVICE
        return audio_cfg.device_id

    def apply(self, audio_cfg: AudioOutputConfig, volume_cfg: VolumeConfig) -> str:
        """Apply current audio & volume config; return ALSA device for renderers."""
        if self.requires_dsp(audio_cfg, volume_cfg):
            logger.info(
                "DSP mode active (software_vol=%s, resample=%s -> %sHz/%sbit)",
                volume_cfg.mode == "software",
                audio_cfg.resample_enabled,
                audio_cfg.resample_rate,
                audio_cfg.resample_bit_depth,
            )
            self.camilla.start(audio_cfg, volume_cfg)
            return LOOPBACK_PLAYBACK_DEVICE

        logger.info("Bit-perfect bypass active -> %s", audio_cfg.device_id)
        self.camilla.stop()
        return audio_cfg.device_id
