"""Apple AirPlay adapter powered by shairport-sync (AirPlay 1 default for ALAC)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

from app.audio.alsa import format_named_alsa_device
from app.config import HydraConfig
from app.renderers.base import RendererAdapter, RendererMetadata


def _get_shairport_conf_path() -> Path:
    cfg_env = os.environ.get("HYDRA_CONFIG_PATH")
    if cfg_env:
        return Path(cfg_env).parent / "shairport-sync.conf"
    etc_dir = Path("/etc/hydrasound")
    if etc_dir.exists() and os.access(etc_dir, os.W_OK):
        return etc_dir / "shairport-sync.conf"
    return Path(__file__).resolve().parents[2] / "data" / "shairport-sync.conf"


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

    def get_installed_version(self) -> Optional[str]:
        try:
            res = subprocess.run(
                [self.meta.binary_name, "-V"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            out = (res.stdout or res.stderr).strip()
            return out.splitlines()[0] if out else None
        except Exception:
            return None

    def write_config(self, cfg: HydraConfig, alsa_device: str) -> Path:
        conf_path = _get_shairport_conf_path()
        conf_path.parent.mkdir(parents=True, exist_ok=True)
        ap = cfg.renderers.airplay
        is_hdmi = "hdmi" in cfg.audio.device_name.lower()
        target_device = format_named_alsa_device(alsa_device, is_hdmi=is_hdmi)
        conf = f"""general = {{
  name = "{ap.device_name}";
  interpolation = "soxr";
  output_backend = "alsa";
  ignore_volume_control = "yes";
}};

alsa = {{
  output_device = "{target_device}";
  output_rate = "auto";
  output_format = "auto";
}};

sessioncontrol = {{
  run_this_before_play_begins = "/opt/hydrasound/scripts/hooks/airplay_event.sh play";
  run_this_after_play_ends = "/opt/hydrasound/scripts/hooks/airplay_event.sh stop";
}};
"""
        conf_path.write_text(conf, encoding="utf-8")
        return conf_path

    def build_command(self, cfg: HydraConfig, alsa_device: str) -> list[str]:
        conf_path = self.write_config(cfg, alsa_device)
        return ["shairport-sync", "-c", str(conf_path)]
