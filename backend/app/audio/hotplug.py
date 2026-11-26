"""USB DAC hotplug monitor using pyudev with stable identity resolution."""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from typing import Awaitable, Callable, Optional

from app.audio.alsa import (
    find_device_by_identity,
    get_usb_device_identity,
    invalidate_usb_cache,
)
from app.config import HydraConfig

logger = logging.getLogger(__name__)

_CARD_NUM_RE = re.compile(r"(?:card|controlC)(\d+)$")


def _extract_card_index(device_id: str) -> Optional[int]:
    if not device_id.startswith("hw:"):
        return None
    try:
        return int(device_id.split(":", 1)[1].split(",", 1)[0])
    except (ValueError, IndexError):
        return None


def _extract_dev_index(device_id: str) -> int:
    if not device_id.startswith("hw:"):
        return 0
    try:
        return int(device_id.split(":", 1)[1].split(",", 1)[1])
    except (ValueError, IndexError):
        return 0


class USBHotplugMonitor:
    """Watches Linux udev sound subsystem events for USB DAC unplug/replug."""

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.usb_disconnected: bool = False

    def start(
        self,
        loop: asyncio.AbstractEventLoop,
        get_cfg: Callable[[], HydraConfig],
        on_disconnect: Callable[[HydraConfig], Awaitable[None]],
        on_reconnect: Callable[[HydraConfig, str], Awaitable[None]],
    ) -> bool:
        """Start background udev monitor thread. Returns False if pyudev/libudev is unavailable."""
        self.stop()
        try:
            import pyudev  # type: ignore

            context = pyudev.Context()
            monitor = pyudev.Monitor.from_netlink(context)
            monitor.filter_by(subsystem="sound")
        except Exception as exc:
            logger.info("USB hotplug monitor unavailable (%s); continuing without udev.", exc)
            return False

        self._stop_event.clear()

        def _watch() -> None:
            logger.info("USB DAC hotplug monitor started (pyudev sound subsystem).")
            while not self._stop_event.is_set():
                try:
                    device = monitor.poll(timeout=0.5)
                except Exception as exc:
                    if not self._stop_event.is_set():
                        logger.warning("USB hotplug poll error: %s", exc)
                    break
                if device is None:
                    continue
                action = getattr(device, "action", None)
                sys_name = getattr(device, "sys_name", "") or ""
                m = _CARD_NUM_RE.search(sys_name)
                if not m:
                    continue
                card_idx = int(m.group(1))

                if action == "remove":
                    invalidate_usb_cache()
                    cfg = get_cfg()
                    active_card = _extract_card_index(cfg.audio.device_id)
                    if active_card is not None and active_card == card_idx and not self.usb_disconnected:
                        self.usb_disconnected = True
                        logger.warning(
                            "Active audio device %s (card %d) unplugged; muting and stopping renderers.",
                            cfg.audio.device_id,
                            card_idx,
                        )
                        asyncio.run_coroutine_threadsafe(on_disconnect(cfg), loop)
                elif action == "add":
                    invalidate_usb_cache()
                    # Wait briefly for ALSA /proc/asound and mixer controls to initialize
                    time.sleep(0.5)
                    cfg = get_cfg()
                    pref_dev = _extract_dev_index(cfg.audio.device_id)
                    matched_id = find_device_by_identity(
                        alsa_card_id=cfg.audio.alsa_card_id,
                        usb_serial=cfg.audio.usb_serial,
                        device_name=cfg.audio.device_name,
                        preferred_dev=pref_dev,
                    )
                    if matched_id is None and _extract_card_index(cfg.audio.device_id) == card_idx:
                        matched_id = cfg.audio.device_id

                    if matched_id and (self.usb_disconnected or matched_id != cfg.audio.device_id):
                        self.usb_disconnected = False
                        logger.info(
                            "USB audio device detected on %s (was %s); restoring pipeline.",
                            matched_id,
                            cfg.audio.device_id,
                        )
                        asyncio.run_coroutine_threadsafe(on_reconnect(cfg, matched_id), loop)

        self._thread = threading.Thread(target=_watch, daemon=True, name="usb-hotplug")
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None


def reconcile_audio_device_identity(cfg: HydraConfig) -> bool:
    """Resolve `cfg.audio.device_id` via stable card_id/serial if card numbers shifted.

    Returns True if `cfg.audio` was updated.
    """
    changed = False
    pref_dev = _extract_dev_index(cfg.audio.device_id)
    if cfg.audio.usb_serial or cfg.audio.alsa_card_id:
        resolved = find_device_by_identity(
            alsa_card_id=cfg.audio.alsa_card_id,
            usb_serial=cfg.audio.usb_serial,
            device_name=cfg.audio.device_name,
            preferred_dev=pref_dev,
        )
        if resolved and resolved != cfg.audio.device_id:
            logger.info(
                "Reconciled shifted audio device %s -> %s (card_id=%s, serial=%s)",
                cfg.audio.device_id,
                resolved,
                cfg.audio.alsa_card_id,
                cfg.audio.usb_serial,
            )
            cfg.audio.device_id = resolved
            changed = True

    card_idx = _extract_card_index(cfg.audio.device_id)
    if card_idx is not None:
        card_id, serial = get_usb_device_identity(card_idx)
        if card_id and card_id != cfg.audio.alsa_card_id:
            cfg.audio.alsa_card_id = card_id
            changed = True
        if serial and serial != cfg.audio.usb_serial:
            cfg.audio.usb_serial = serial
            changed = True
    return changed
