"""Qobuz Connect adapter powered by pibuz."""

from __future__ import annotations

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
        cmd = [
            "pibuz",
            "--name",
            qc.device_name,
            "--alsa-device",
            alsa_device,
            "--on-event",
            "/opt/hydrasound/scripts/hooks/pibuz_event.sh",
        ]
        if qc.email and qc.password:
            cmd.extend(["--email", qc.email, "--password", qc.password])
        return cmd
