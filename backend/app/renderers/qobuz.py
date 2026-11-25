"""Qobuz Connect adapter powered by pibuz."""

from __future__ import annotations

import subprocess

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
        target_device = alsa_device
        if (
            "hdmi" in cfg.audio.device_name.lower()
            and alsa_device.startswith("hw:")
            and not alsa_device.startswith("hw:Loopback")
        ):
            card_dev = alsa_device.split(":", 1)[1].split(",")
            if len(card_dev) == 2:
                target_device = f"hdmi:CARD={card_dev[0]},DEV={card_dev[1]}"

        # Apply device name & ALSA settings via pibuz settings CLI before running daemon
        try:
            subprocess.run(
                ["pibuz", "settings", "set", "qconnect.device_name", qc.device_name],
                capture_output=True,
                timeout=3,
            )
            subprocess.run(
                ["pibuz", "settings", "set", "audio.device", target_device],
                capture_output=True,
                timeout=3,
            )
        except Exception:
            pass
        return ["pibuz", "run"]
