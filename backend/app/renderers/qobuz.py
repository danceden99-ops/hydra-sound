"""Qobuz Connect adapter powered by pibuz."""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
import urllib.request
from typing import Callable, Optional

from app.audio.alsa import format_named_alsa_device
from app.config import HydraConfig
from app.renderers.base import RendererAdapter, RendererMetadata

_STREAM_INFO_RE = re.compile(r"(\d+)Hz,\s*\d+\s*(?:ch|channels?),\s*(\d+)-bit")
_VOLUME_RE = re.compile(r"Renderer updated:.*?volume=Some\((\d+)\)")


def _post_qobuz_event(payload: dict) -> None:
    def _send() -> None:
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

    threading.Thread(target=_send, daemon=True).start()


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

    def __init__(self) -> None:
        super().__init__()
        self._last_rate: Optional[int] = None
        self._last_bits: Optional[int] = None
        self._on_client_volume: Optional[Callable[[int], None]] = None
        self._last_pushed_at: float = 0.0
        self._last_reported_vol: Optional[int] = None

    def set_volume_callback(self, cb: Optional[Callable[[int], None]]) -> None:
        """Register callback invoked when the Qobuz app changes volume."""
        self._on_client_volume = cb

    def set_client_volume(self, volume_percent: int, muted: bool) -> None:
        """Push Hydra volume/mute state to pibuz so the Qobuz app slider stays in sync."""
        if not self.is_running():
            return
        self._last_pushed_at = time.monotonic()
        target = max(0, min(100, int(volume_percent)))
        self._last_reported_vol = target
        payload = json.dumps({"volume": target, "mute": bool(muted)}).encode("utf-8")

        def _send() -> None:
            try:
                req = urllib.request.Request(
                    "http://127.0.0.1:8182/api/playback/volume",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=2):
                    pass
            except Exception:
                pass

        threading.Thread(target=_send, daemon=True).start()

    def is_enabled(self, cfg: HydraConfig) -> bool:
        return cfg.renderers.qobuz.enabled

    def _should_log_line(self, line: str) -> bool:
        if " INFO " in line and (
            "[QConnect/Decode]" in line
            or "[QConnect/Transport]" in line
            or "[QConnect] Session management:" in line
            or "Renderer command applied:" in line
        ):
            return False
        return True

    def _update_connection_state(self, line: str) -> None:
        super()._update_connection_state(line)
        if "FILE_AUDIO_QUALITY_CHANGED" in line and "payload=" in line:
            try:
                payload = json.loads(line.split("payload=", 1)[1].strip())
                rate = payload.get("sampling_rate")
                bits = payload.get("bit_depth")
                if isinstance(rate, int):
                    self._last_rate = rate
                if isinstance(bits, int):
                    self._last_bits = bits
                _post_qobuz_event(
                    {
                        "source": "qobuz",
                        "event": "metadata",
                        "incoming_format": "FLAC",
                        "incoming_rate": self._last_rate,
                        "incoming_bit_depth": self._last_bits,
                    }
                )
            except Exception:
                pass
            return

        m = _STREAM_INFO_RE.search(line)
        if m:
            self._last_rate = int(m.group(1))
            self._last_bits = int(m.group(2))

        if "Audio thread: playing track" in line or "Audio thread: resumed" in line:
            self.connection_status = "connected"
            self.connection_detail = "Connected & playing"
            _post_qobuz_event(
                {
                    "source": "qobuz",
                    "event": "play",
                    "incoming_format": "FLAC",
                    "incoming_rate": self._last_rate,
                    "incoming_bit_depth": self._last_bits,
                }
            )
        elif "Audio thread: paused" in line:
            self.connection_status = "connected"
            self.connection_detail = "Paused"
            _post_qobuz_event({"source": "qobuz", "event": "pause"})
        elif "Audio thread: suspended" in line:
            self.connection_status = "advertising"
            self.connection_detail = "Discoverable on LAN"
            _post_qobuz_event({"source": "qobuz", "event": "stop"})

        vol_match = _VOLUME_RE.search(line)
        if vol_match:
            vol = max(0, min(100, int(vol_match.group(1))))
            if (
                self._on_client_volume is not None
                and vol != self._last_reported_vol
                and (time.monotonic() - self._last_pushed_at) > 0.15
            ):
                self._last_reported_vol = vol
                self._on_client_volume(vol)

    def build_command(self, cfg: HydraConfig, alsa_device: str) -> list[str]:
        qc = cfg.renderers.qobuz
        if alsa_device == "camilladsp":
            alsa_plugin = "hw"
            target_device = "camilladsp"
        else:
            is_loopback = "loopback" in alsa_device.lower()
            is_hdmi = "hdmi" in cfg.audio.device_name.lower()
            target_device = format_named_alsa_device(alsa_device, is_hdmi=is_hdmi)
            alsa_plugin = "plughw" if is_loopback else "hw"

        # Apply device name, volume mode & ALSA settings via pibuz settings CLI before running daemon
        volume_mode = "locked" if cfg.volume.mode == "disabled" else "external"
        for key, value in (
            ("qconnect.device_name", qc.device_name),
            ("qconnect.volume_mode", volume_mode),
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
