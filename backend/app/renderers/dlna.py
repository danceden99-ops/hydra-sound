"""DLNA/UPnP adapter powered by gmrender-resurrect."""

from __future__ import annotations

from app.audio.alsa import format_named_alsa_device
from app.config import HydraConfig
from app.renderers.base import RendererAdapter, RendererMetadata


class DLNARenderer(RendererAdapter):
    meta = RendererMetadata(
        id="dlna",
        display_name="DLNA / UPnP",
        binary_name="gmediarender",
        github_repo="hzeller/gmrender-resurrect",
        update_script="scripts/modules/update_dlna.sh",
        quality_note="Open standard supporting bit-perfect Hi-Res PCM/FLAC up to DAC limits.",
    )

    def is_enabled(self, cfg: HydraConfig) -> bool:
        return cfg.renderers.dlna.enabled

    def build_command(self, cfg: HydraConfig, alsa_device: str) -> list[str]:
        dc = cfg.renderers.dlna
        is_hdmi = "hdmi" in cfg.audio.device_name.lower()
        target_device = format_named_alsa_device(alsa_device, is_hdmi=is_hdmi)
        return [
            "gmediarender",
            "--friendly-name",
            dc.device_name,
            "--gstout-audiosink",
            "alsasink",
            "--gstout-audiodevice",
            target_device,
            "--gstout-initial-volume-db",
            "0",
        ]
