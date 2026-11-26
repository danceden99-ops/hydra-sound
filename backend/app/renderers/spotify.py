"""Spotify Connect adapter powered by go-librespot (with librespot fallback)."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional

import yaml

from app.audio.alsa import is_usb_audio_device, select_best_pcm_format
from app.config import HydraConfig
from app.renderers.base import RendererAdapter, RendererMetadata

logger = logging.getLogger(__name__)

GO_LIBRESPOT_CONFIG_DIR = Path("/tmp/hydra-go-librespot")
GO_LIBRESPOT_API_PORT = 3678


def _format_target_device(alsa_device: str, device_name: str) -> str:
    """Apply IEC958 framing for Raspberry Pi vc4-hdmi outputs."""
    if (
        "hdmi" in device_name.lower()
        and alsa_device.startswith("hw:")
        and not alsa_device.startswith("hw:Loopback")
    ):
        card_dev = alsa_device.split(":", 1)[1].split(",")
        if len(card_dev) == 2:
            return f"hdmi:CARD={card_dev[0]},DEV={card_dev[1]}"
    return alsa_device


def _post_internal_event(payload: dict) -> None:
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8000/api/internal/event",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2):
            pass
    except Exception:
        pass


class SpotifyRenderer(RendererAdapter):
    meta = RendererMetadata(
        id="spotify",
        display_name="Spotify Connect",
        binary_name="go-librespot",
        github_repo="devgianlu/go-librespot",
        update_script="scripts/modules/update_librespot.sh",
        quality_note=(
            "Requires Spotify Premium. Uses go-librespot for Now Playing artwork "
            "and bidirectional volume sync (falls back to librespot if absent)."
        ),
    )

    def __init__(self) -> None:
        super().__init__()
        self._using_go_librespot: bool = False
        self._ws_stop = threading.Event()
        self._ws_thread: Optional[threading.Thread] = None
        self._ws_conn = None
        self._on_client_volume: Optional[Callable[[int], None]] = None
        self._last_pushed_at: float = 0.0
        self._go_librespot_ok: Optional[tuple[float, bool]] = None

    def set_volume_callback(self, cb: Optional[Callable[[int], None]]) -> None:
        """Register callback invoked when the Spotify app changes volume."""
        self._on_client_volume = cb

    def is_enabled(self, cfg: HydraConfig) -> bool:
        return cfg.renderers.spotify.enabled

    def _update_connection_state(self, line: str) -> None:
        super()._update_connection_state(line)
        if self._using_go_librespot and (
            "output device failed" in line or "failed starting playback: ALSA error" in line
        ):
            def _retry_play() -> None:
                time.sleep(0.8)
                if not self.is_running():
                    return
                try:
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{GO_LIBRESPOT_API_PORT}/player/resume",
                        data=b"{}",
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=2):
                        pass
                except Exception:
                    pass

            threading.Thread(target=_retry_play, daemon=True).start()

    def _has_go_librespot(self) -> bool:
        now = time.monotonic()
        if self._go_librespot_ok and now - self._go_librespot_ok[0] < 60.0:
            return self._go_librespot_ok[1]
        if shutil.which("go-librespot") is None:
            self._go_librespot_ok = (now, False)
            return False
        try:
            res = subprocess.run(
                ["go-librespot", "-h"], capture_output=True, text=True, timeout=2
            )
            err = (res.stderr or "") + (res.stdout or "")
            ok = res.returncode != 127 and "error while loading shared libraries" not in err
            if not ok:
                logger.warning(
                    "go-librespot binary cannot start (%s); falling back to librespot.",
                    err.strip().splitlines()[0] if err.strip() else res.returncode,
                )
        except Exception as exc:
            logger.warning("go-librespot check failed (%s); falling back to librespot.", exc)
            ok = False
        self._go_librespot_ok = (now, ok)
        return ok

    def _write_go_librespot_config(self, cfg: HydraConfig, target_device: str) -> Path:
        sc = cfg.renderers.spotify
        GO_LIBRESPOT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        config_data = {
            "device_name": sc.device_name,
            "device_type": "speaker",
            "bitrate": sc.bitrate,
            "audio_backend": "alsa",
            "audio_device": target_device,
            "audio_buffer_time": 500000,
            "audio_period_count": 4,
            "external_volume": True,
            "initial_volume": 100,
            "volume_steps": 100,
            "zeroconf_enabled": sc.auth_mode == "zeroconf",
            "server": {
                "enabled": True,
                "address": "127.0.0.1",
                "port": GO_LIBRESPOT_API_PORT,
            },
        }
        cfg_file = GO_LIBRESPOT_CONFIG_DIR / "config.yml"
        cfg_file.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
        return GO_LIBRESPOT_CONFIG_DIR

    def build_command(self, cfg: HydraConfig, alsa_device: str) -> list[str]:
        sc = cfg.renderers.spotify
        target_device = _format_target_device(alsa_device, cfg.audio.device_name)

        if self._has_go_librespot():
            self._using_go_librespot = True
            cfg_dir = self._write_go_librespot_config(cfg, target_device)
            return ["go-librespot", "--config_dir", str(cfg_dir)]

        self._using_go_librespot = False
        cmd = [
            "librespot",
            "--name",
            sc.device_name,
            "--backend",
            "alsa",
            "--device",
            target_device,
            "--bitrate",
            str(sc.bitrate),
            "--volume-ctrl",
            "fixed",
            "--initial-volume",
            "100",
            "--onevent",
            "/opt/hydrasound/scripts/hooks/librespot_event.sh",
        ]
        best_fmt = select_best_pcm_format(alsa_device)
        if "loopback" in alsa_device.lower() or best_fmt == "S32_LE" or is_usb_audio_device(
            alsa_device, cfg.audio.device_name
        ):
            cmd.extend(["--format", "S32", "--dither", "none"])
        elif best_fmt in ("S24_3_LE", "S24_4_RJ_LE"):
            cmd.extend(["--format", "S24_3", "--dither", "none"])

        if sc.auth_mode == "credentials" and sc.username and sc.password:
            cmd.extend(["--username", sc.username, "--password", sc.password])
        return cmd

    def start(self, cfg: HydraConfig, alsa_device: str) -> bool:
        started = super().start(cfg, alsa_device)
        if started and self._using_go_librespot:
            self._start_events_listener()
        return started

    def stop(self) -> None:
        self._stop_events_listener()
        super().stop()

    def _start_events_listener(self) -> None:
        self._stop_events_listener()
        self._ws_stop.clear()

        def _listen() -> None:
            try:
                from websockets.sync.client import connect
            except Exception as exc:
                logger.warning("websockets.sync unavailable for go-librespot: %s", exc)
                return

            ws_url = f"ws://127.0.0.1:{GO_LIBRESPOT_API_PORT}/events"
            while not self._ws_stop.is_set() and self.is_running():
                try:
                    with connect(ws_url, open_timeout=3) as ws:
                        self._ws_conn = ws
                        while not self._ws_stop.is_set() and self.is_running():
                            raw = ws.recv()
                            if not raw:
                                continue
                            self._handle_ws_event(json.loads(raw))
                except Exception:
                    if self._ws_stop.wait(1.0):
                        break
                finally:
                    self._ws_conn = None

        self._ws_thread = threading.Thread(
            target=_listen, daemon=True, name="go-librespot-ws"
        )
        self._ws_thread.start()

    def _stop_events_listener(self) -> None:
        self._ws_stop.set()
        conn = self._ws_conn
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=1.0)
        self._ws_thread = None

    def _handle_ws_event(self, event: dict) -> None:
        etype = event.get("type", "")
        data = event.get("data") if isinstance(event.get("data"), dict) else event
        if etype in ("active", "playing"):
            self.connection_status = "connected"
            self.connection_detail = "Connected & playing"
            _post_internal_event({"source": "spotify", "event": "play"})
        elif etype == "paused":
            _post_internal_event({"source": "spotify", "event": "pause"})
        elif etype in ("inactive", "stopped"):
            _post_internal_event({"source": "spotify", "event": "stop"})
        elif etype == "metadata":
            artists = data.get("artist_names") or []
            artist = (
                ", ".join(str(a) for a in artists)
                if isinstance(artists, list)
                else str(artists)
            )
            bitrate = data.get("bitrate")
            fmt = "FLAC" if bitrate is None else f"Ogg Vorbis ({bitrate} kbps)"
            _post_internal_event(
                {
                    "source": "spotify",
                    "event": "metadata",
                    "title": str(data.get("name") or data.get("title") or ""),
                    "artist": artist,
                    "album": str(data.get("album_name") or ""),
                    "artwork_url": str(data.get("album_cover_url") or ""),
                    "incoming_format": fmt,
                    "incoming_rate": 44100,
                    "incoming_bit_depth": 16,
                }
            )
        elif etype == "volume":
            if time.monotonic() - self._last_pushed_at < 0.5:
                return
            val = data.get("value")
            max_val = data.get("max") or 100
            if isinstance(val, (int, float)) and max_val:
                pct = max(0, min(100, round((float(val) / float(max_val)) * 100)))
                if self._on_client_volume:
                    self._on_client_volume(pct)

    def set_client_volume(self, volume_percent: int, muted: bool) -> None:
        """Push Hydra volume changes back to go-librespot over its REST API."""
        if not self._using_go_librespot or not self.is_running():
            return
        self._last_pushed_at = time.monotonic()
        target_vol = 0 if muted else max(0, min(100, int(volume_percent)))

        def _push() -> None:
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{GO_LIBRESPOT_API_PORT}/player/volume",
                    data=json.dumps({"volume": target_vol, "relative": False}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=2):
                    pass
            except Exception:
                pass

        threading.Thread(target=_push, daemon=True).start()

    def get_installed_version(self) -> Optional[str]:
        if self._has_go_librespot():
            ver_file = Path("/usr/local/bin/go-librespot.version")
            if ver_file.is_file():
                ver = ver_file.read_text(encoding="utf-8").strip()
                if ver:
                    return f"go-librespot {ver}"
            self.meta.binary_name = "go-librespot"
            out = super().get_installed_version()
            if out and "unknown flag" not in out.lower() and "usage" not in out.lower():
                return f"go-librespot {out}"
            return "go-librespot (installed)"
        self.meta.binary_name = "librespot"
        return super().get_installed_version()
