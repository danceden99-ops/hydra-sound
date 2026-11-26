"""Apple AirPlay adapter powered by shairport-sync (AirPlay 1 default for ALAC)."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import threading
import urllib.request
from pathlib import Path
from typing import Optional

from app.audio.alsa import format_named_alsa_device
from app.config import HydraConfig
from app.renderers.base import RendererAdapter, RendererMetadata

SHAIRPORT_METADATA_PIPE = Path("/tmp/shairport-sync-metadata")
_ITEM_RE = re.compile(
    r"<item><type>([0-9a-fA-F]+)</type><code>([0-9a-fA-F]+)</code><length>\d+</length>"
    r"(?:<data encoding=\"base64\">\s*([^<]*?)\s*</data>)?</item>"
)


def _post_airplay_metadata(title: str, artist: str, album: str) -> None:
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8000/api/internal/event",
            data=json.dumps(
                {
                    "source": "airplay",
                    "event": "metadata",
                    "title": title,
                    "artist": artist,
                    "album": album,
                    "incoming_format": "ALAC (Lossless)",
                    "incoming_rate": 44100,
                    "incoming_bit_depth": 16,
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2):
            pass
    except Exception:
        pass


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

    def __init__(self) -> None:
        super().__init__()
        self._meta_stop = threading.Event()
        self._meta_thread: Optional[threading.Thread] = None

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
  wait_for_completion = "yes";
}};

metadata = {{
  enabled = "yes";
  include_cover_art = "no";
  pipe_name = "{SHAIRPORT_METADATA_PIPE}";
  pipe_timeout = 5000;
}};
"""
        conf_path.write_text(conf, encoding="utf-8")
        return conf_path

    def build_command(self, cfg: HydraConfig, alsa_device: str) -> list[str]:
        conf_path = self.write_config(cfg, alsa_device)
        return ["shairport-sync", "-c", str(conf_path)]

    def start(self, cfg: HydraConfig, alsa_device: str) -> bool:
        try:
            if not SHAIRPORT_METADATA_PIPE.exists() and hasattr(os, "mkfifo"):
                os.mkfifo(SHAIRPORT_METADATA_PIPE)
        except Exception:
            pass
        started = super().start(cfg, alsa_device)
        if started:
            self._start_metadata_reader()
        return started

    def stop(self) -> None:
        self._meta_stop.set()
        super().stop()

    def _start_metadata_reader(self) -> None:
        self._meta_stop.clear()
        if self._meta_thread and self._meta_thread.is_alive():
            return

        def _read_pipe() -> None:
            cur = {"title": "", "artist": "", "album": ""}
            while not self._meta_stop.is_set() and self.is_running():
                try:
                    if not SHAIRPORT_METADATA_PIPE.exists():
                        self._meta_stop.wait(1.0)
                        continue
                    with SHAIRPORT_METADATA_PIPE.open("r", encoding="utf-8", errors="replace") as f:
                        buf = ""
                        while not self._meta_stop.is_set() and self.is_running():
                            line = f.readline()
                            if not line:
                                break
                            buf += line
                            if "</item>" not in buf:
                                continue
                            m = _ITEM_RE.search(buf)
                            buf = ""
                            if not m:
                                continue
                            code = m.group(2).lower()
                            raw_b64 = m.group(3) or ""
                            val = ""
                            if raw_b64:
                                try:
                                    val = base64.b64decode(raw_b64).decode("utf-8", errors="replace").strip()
                                except Exception:
                                    pass
                            # core: mdst=6d647374 (start), minm=6d696e6d (title), asar=61736172 (artist), asal=6173616c (album)
                            if code == "6d647374":
                                cur = {"title": "", "artist": "", "album": ""}
                            elif code == "6d696e6d":
                                cur["title"] = val
                            elif code == "61736172":
                                cur["artist"] = val
                            elif code == "6173616c":
                                cur["album"] = val
                            elif code == "6d64656e":  # mden (metadata bundle end)
                                if any(cur.values()):
                                    self.connection_status = "connected"
                                    self.connection_detail = "Connected & playing"
                                    _post_airplay_metadata(cur["title"], cur["artist"], cur["album"])
                except Exception:
                    if self._meta_stop.wait(1.0):
                        break

        self._meta_thread = threading.Thread(
            target=_read_pipe, daemon=True, name="airplay-metadata"
        )
        self._meta_thread.start()
