"""Apple AirPlay adapter powered by shairport-sync (AirPlay 1 default for ALAC)."""

from __future__ import annotations

from pathlib import Path

from app.config import HydraConfig
from app.renderers.base import RendererAdapter, RendererMetadata

SHAIRPORT_CONF = Path("/etc/hydrasound/shairport-sync.conf")


class AirPlayRenderer(RendererAdapter):
    meta = RendererMetadata(
        id="airplay",
        display_name="Apple AirPlay",
        binary_name="shairport-sync",
        github_repo="mikebrady/shairport-sync",
        update_script="scripts/modules/update_shairport.sh",
        quality_note=(
            "Configured for AirPlay 1 (lossless 16-bit/44.1 kHz ALAC). "
            "AirPlay 2 often transcodes to 256 kbps AAC depending on sender."
        ),
    )

    def is_enabled(self, cfg: HydraConfig) -> bool:
        return cfg.renderers.airplay.enabled

    def write_config(self, cfg: HydraConfig, alsa_device: str) -> Path:
        SHAIRPORT_CONF.parent.mkdir(parents=True, exist_ok=True)
        ap = cfg.renderers.airplay
        ignore_vol = "yes" if cfg.volume.mode == "disabled" else "no"
        conf = f"""general = {{
  name = "{ap.device_name}";
  interpolation = "soxr";
  output_backend = "alsa";
  ignore_volume_control = "{ignore_vol}";
}};

alsa = {{
  output_device = "{alsa_device}";
  output_rate = "auto";
  output_format = "auto";
}};

sessioncontrol = {{
  run_this_before_play_begins = "/opt/hydrasound/scripts/hooks/airplay_event.sh play";
  run_this_after_play_ends = "/opt/hydrasound/scripts/hooks/airplay_event.sh stop";
}};
"""
        SHAIRPORT_CONF.write_text(conf, encoding="utf-8")
        return SHAIRPORT_CONF

    def build_command(self, cfg: HydraConfig, alsa_device: str) -> list[str]:
        conf_path = self.write_config(cfg, alsa_device)
        return ["shairport-sync", "-c", str(conf_path)]
