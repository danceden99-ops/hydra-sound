"""Qobuz Connect adapter powered by pibuz."""

from __future__ import annotations

import json
import re
import subprocess
import threading
import urllib.request
from typing import Optional

from app.audio.alsa import format_named_alsa_device
from app.config import HydraConfig
from app.renderers.base import RendererAdapter, RendererMetadata

_STREAM_INFO_RE = re.compile(r"(\d+)Hz,\s*\d+\s*(?:ch|channels?),\s*(\d+)-bit")


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

    def is_enabled(self, cfg: HydraConfig) -> bool:
        return cfg.renderers.qobuz.enabled

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
            _post_qobuz_event({"source": "qobuz", "event": "pause"})
        elif "Audio thread: suspended" in line:
            _post_qobuz_event({"source": "qobuz", "event": "stop"})

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
