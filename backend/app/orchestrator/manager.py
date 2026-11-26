"""First-come-first-serve Stream Orchestrator.

Enforces single active stream ownership:
- Stop / disconnect immediately releases the lock.
- Pause starts a configurable timeout; when expired, releases the lock.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, Optional

from app.audio.alsa import probe_active_hw_params
from app.audio.pipeline import AudioPipelineManager
from app.config import HydraConfig
from app.renderers.airplay import AirPlayRenderer
from app.renderers.base import RendererAdapter
from app.renderers.dlna import DLNARenderer
from app.renderers.googlecast import GoogleCastRenderer
from app.renderers.qobuz import QobuzRenderer
from app.renderers.spotify import SpotifyRenderer

logger = logging.getLogger(__name__)


@dataclass
class StreamInfo:
    active_source: Optional[str] = None
    state: str = "idle"  # "idle" | "playing" | "paused"
    artist: str = ""
    title: str = ""
    album: str = ""
    artwork_url: str = ""
    incoming_format: str = ""
    incoming_rate: Optional[int] = None
    incoming_bit_depth: Optional[int] = None
    outgoing_format: str = ""
    outgoing_rate: Optional[int] = None
    outgoing_bit_depth: Optional[int] = None
    pause_remaining_seconds: Optional[int] = None


class StreamOrchestrator:
    """Coordinates renderer lifecycles and first-come-first-serve stream locking."""

    def __init__(self, pipeline: AudioPipelineManager) -> None:
        self.pipeline = pipeline
        self.renderers: Dict[str, RendererAdapter] = {
            "spotify": SpotifyRenderer(),
            "qobuz": QobuzRenderer(),
            "airplay": AirPlayRenderer(),
            "dlna": DLNARenderer(),
            "googlecast": GoogleCastRenderer(),
        }
        self.stream = StreamInfo()
        self._pause_task: Optional[asyncio.Task] = None
        self._current_alsa_device: str = "hw:0,0"
        self._active_capture_rate: int = 44100
        self._on_pipeline_restarted: Optional[Callable[[HydraConfig], Awaitable[None]]] = None

    def set_volume_reapply_callback(
        self, cb: Optional[Callable[[HydraConfig], Awaitable[None]]]
    ) -> None:
        """Register async callback to re-apply volume after CamillaDSP rate-change restart."""
        self._on_pipeline_restarted = cb

    def start_enabled_renderers(self, cfg: HydraConfig) -> None:
        self._current_alsa_device = self.pipeline.apply(cfg.audio, cfg.volume)
        for rid, renderer in self.renderers.items():
            if renderer.is_enabled(cfg):
                renderer.start(cfg, self._current_alsa_device)
            else:
                renderer.stop()
                if self.stream.active_source == rid:
                    self._release_lock()

    def stop_all(self) -> None:
        self._cancel_pause_timer()
        for renderer in self.renderers.values():
            renderer.stop()
        self.pipeline.camilla.stop()
        self._release_lock()

    async def handle_renderer_event(
        self,
        source: str,
        event: str,
        cfg: HydraConfig,
        artist: str = "",
        title: str = "",
        album: str = "",
        artwork_url: str = "",
        incoming_format: str = "",
        incoming_rate: Optional[int] = None,
        incoming_bit_depth: Optional[int] = None,
    ) -> dict:
        """Process play/pause/stop/metadata events from renderer hook scripts."""
        if source not in self.renderers:
            return {"accepted": False, "reason": "unknown_source"}

        # Stream lock / preemption check
        if (
            self.stream.active_source is not None
            and self.stream.active_source != source
            and self.stream.state in ("playing", "paused")
        ):
            if event not in ("play", "playing", "start", "metadata"):
                return {"accepted": False, "reason": "not_active_source"}

            lock_mode = getattr(cfg.renderers, "lock_mode", "preempt_paused")
            can_preempt = (
                lock_mode == "last_wins"
                or (lock_mode == "preempt_paused" and self.stream.state == "paused")
            )
            if not can_preempt:
                logger.info(
                    "Rejected stream request from %s; locked by %s (%s)",
                    source,
                    self.stream.active_source,
                    self.stream.state,
                )
                return {
                    "accepted": False,
                    "reason": "locked",
                    "active_source": self.stream.active_source,
                }

            prev = self.stream.active_source
            logger.info(
                "Preempting %s (%s) in favor of %s (mode=%s)",
                prev,
                self.stream.state,
                source,
                lock_mode,
            )
            self._cancel_pause_timer()
            self._release_lock()
            if prev in self.renderers and self.renderers[prev].is_enabled(cfg):
                self.renderers[prev].stop()
                await asyncio.sleep(0.4)
                self.renderers[prev].start(cfg, self._current_alsa_device)

        if event in ("play", "playing", "start", "metadata"):
            self._cancel_pause_timer()
            if self.stream.active_source != source:
                self.stream = StreamInfo(active_source=source, state="playing")
            elif event != "metadata":
                self.stream.state = "playing"
            self.stream.active_source = source
            if artist:
                self.stream.artist = artist
            if title:
                self.stream.title = title
            if album:
                self.stream.album = album
            if artwork_url:
                self.stream.artwork_url = artwork_url
            if incoming_format:
                self.stream.incoming_format = incoming_format
            if incoming_rate:
                self.stream.incoming_rate = incoming_rate
                if (
                    8000 <= incoming_rate <= 768000
                    and incoming_rate != self._active_capture_rate
                    and self.pipeline.requires_dsp(cfg.audio, cfg.volume)
                ):
                    self._active_capture_rate = incoming_rate
                    self.pipeline.apply(
                        cfg.audio, cfg.volume, capture_rate=incoming_rate
                    )
                    if self._on_pipeline_restarted:
                        await self._on_pipeline_restarted(cfg)
            if incoming_bit_depth:
                self.stream.incoming_bit_depth = incoming_bit_depth
            self.refresh_outgoing_format(cfg)
            return {"accepted": True, "state": "playing", "active_source": source}

        if event == "pause" and self.stream.active_source == source:
            self.stream.state = "paused"
            self._start_pause_timer(cfg.renderers.pause_timeout_seconds, cfg)
            return {"accepted": True, "state": "paused", "active_source": source}

        if event in ("stop", "stopped", "disconnect") and self.stream.active_source == source:
            self._cancel_pause_timer()
            self._release_lock()
            return {"accepted": True, "state": "idle", "active_source": None}

        return {"accepted": True, "state": self.stream.state}

    def force_release_lock(self, cfg: HydraConfig) -> None:
        """Manually release lock (e.g. from Web GUI) and restart that renderer cleanly."""
        prev = self.stream.active_source
        self._cancel_pause_timer()
        self._release_lock()
        if prev and prev in self.renderers and self.renderers[prev].is_enabled(cfg):
            self.renderers[prev].start(cfg, self._current_alsa_device)

    def _release_lock(self) -> None:
        self.stream = StreamInfo()

    def _cancel_pause_timer(self) -> None:
        if self._pause_task and not self._pause_task.done():
            self._pause_task.cancel()
        self._pause_task = None
        self.stream.pause_remaining_seconds = None

    def _start_pause_timer(self, timeout_seconds: int, cfg: HydraConfig) -> None:
        self._cancel_pause_timer()

        async def _countdown() -> None:
            remaining = timeout_seconds
            while remaining > 0:
                self.stream.pause_remaining_seconds = remaining
                await asyncio.sleep(1)
                remaining -= 1
            logger.info(
                "Pause timeout (%ss) expired for %s; soft-releasing stream lock.",
                timeout_seconds,
                self.stream.active_source,
            )
            self._release_lock()

        try:
            loop = asyncio.get_running_loop()
            self._pause_task = loop.create_task(_countdown())
        except RuntimeError:
            pass

    def refresh_outgoing_format(self, cfg: HydraConfig) -> None:
        hw = probe_active_hw_params(cfg.audio.device_id)
        if hw.rate:
            self.stream.outgoing_rate = hw.rate
            self.stream.outgoing_bit_depth = hw.bit_depth
            self.stream.outgoing_format = hw.format or ""
        elif cfg.audio.resample_enabled:
            self.stream.outgoing_rate = cfg.audio.resample_rate
            self.stream.outgoing_bit_depth = cfg.audio.resample_bit_depth
            self.stream.outgoing_format = f"S{cfg.audio.resample_bit_depth}_LE"
        else:
            self.stream.outgoing_rate = self.stream.incoming_rate
            self.stream.outgoing_bit_depth = self.stream.incoming_bit_depth
            self.stream.outgoing_format = self.stream.incoming_format

    def push_volume_to_active_client(self, volume_percent: int, muted: bool) -> None:
        if self.stream.active_source and self.stream.active_source in self.renderers:
            self.renderers[self.stream.active_source].set_client_volume(
                volume_percent, muted
            )

    def get_renderers_status(self, cfg: HydraConfig) -> Dict[str, dict]:
        result: Dict[str, dict] = {}
        for rid, adapter in self.renderers.items():
            enabled = adapter.is_enabled(cfg)
            result[rid] = {
                "display_name": adapter.meta.display_name,
                "enabled": enabled,
                "running": adapter.is_running(),
                "status": adapter.connection_status if enabled else "disabled",
                "detail": adapter.connection_detail if enabled else "Disabled",
            }
        return result
