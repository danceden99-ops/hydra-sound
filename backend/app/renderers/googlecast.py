"""Optional Google Cast adapter powered by shanocast (disabled by default)."""

from __future__ import annotations

from app.config import HydraConfig
from app.renderers.base import RendererAdapter, RendererMetadata


class GoogleCastRenderer(RendererAdapter):
    meta = RendererMetadata(
        id="googlecast",
        display_name="Google Cast (Experimental)",
        binary_name="shanocast",
        github_repo="rgerganov/shanocast",
        update_script="scripts/modules/update_shanocast.sh",
        quality_note=(
            "Disabled by default. Google Cast requires Google-signed hardware DRM "
            "certificates; native mobile apps often reject unofficial receivers. "
            "Best-effort for desktop Chrome tab casting."
        ),
    )

    def is_enabled(self, cfg: HydraConfig) -> bool:
        return cfg.renderers.googlecast.enabled

    def build_command(self, cfg: HydraConfig, alsa_device: str) -> list[str]:
        gc = cfg.renderers.googlecast
        return [
            "shanocast",
            "--name",
            gc.device_name,
            "--interface",
            cfg.network.interface,
            "--alsa-device",
            alsa_device,
        ]
