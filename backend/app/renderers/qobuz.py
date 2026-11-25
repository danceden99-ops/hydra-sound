"""Qobuz Connect adapter powered by pibuz."""

from __future__ import annotations

import subprocess

from app.audio.alsa import format_named_alsa_device
from app.config import HydraConfig
from app.renderers.base import RendererAdapter, RendererMetadata


class QobuzRenderer(RendererAdapter):
    meta = RendererMetadata(
        id="qobuz",
        display_name="Qobuz Connect",
        binary_name="pibuz",
        github_repo="PhilipVinc/pibuz",
        update_script="scripts/modules/update_pibuz.sh",
        quality_note=(
            "Supports bit-perfect Hi-Res FLAC up to 24-bit/192 kHz. "
            "Requires an active Qobuz subscription."
        ),
    )

    def is_enabled(self, cfg: HydraConfig) -> bool:
        return cfg.renderers.qobuz.enabled

    def build_command(self, cfg: HydraConfig, alsa_device: str) -> list[str]:
        qc = cfg.renderers.qobuz
        is_loopback = "loopback" in alsa_device.lower()
        is_hdmi = "hdmi" in cfg.audio.device_name.lower()
        target_device = format_named_alsa_device(alsa_device, is_hdmi=is_hdmi)

        alsa_plugin = "plughw" if is_loopback else "hw"

        # Apply device name & ALSA settings via pibuz settings CLI before running daemon
        for key, value in (
            ("qconnect.device_name", qc.device_name),
            ("audio.backend", "alsa"),
            ("audio.alsa_plugin", alsa_plugin),
            ("audio.device", target_device),
        ):
            try:
                subprocess.run(
                    ["pibuz", "settings", "set", key, value],
                    capture_output=True,
                    timeout=3,
                )
            except Exception:
                pass
        return ["pibuz", "run"]
