"""IR Receiver Manager for FLIRC USB (with Web GUI learning) and GPIO IR."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import select
import struct
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional

from app.config import IRConfig

logger = logging.getLogger(__name__)

BOOT_CONFIG_PATHS = (
    Path("/boot/firmware/config.txt"),
    Path("/boot/config.txt"),
)

FLIRC_ACTION_TO_KEY = {
    "vol_up": "vol_up",
    "vol_down": "vol_down",
    "mute": "mute",
}

_EV_KEY = 0x01
_EV_MSC = 0x04
_MSC_SCAN = 0x04
_KEY_MUTE = 113
_KEY_VOLUMEDOWN = 114
_KEY_VOLUMEUP = 115
_INPUT_EVENT_FMT = "llHHi"
_INPUT_EVENT_SIZE = struct.calcsize(_INPUT_EVENT_FMT)


def _enable_gpio_ir_protocols() -> List[str]:
    """Enable standard consumer IR decoders and disable noisy ones (imon/mce_kbd) on /sys/class/rc/rc*."""
    enabled: List[str] = []
    noisy = {"lirc", "imon", "mce_kbd", "cec"}
    rc_root = Path("/sys/class/rc")
    if not rc_root.exists():
        return enabled
    for proto_file in sorted(rc_root.glob("rc*/protocols")):
        try:
            text = proto_file.read_text(encoding="utf-8", errors="ignore")
            for tok in text.split():
                clean = tok.strip("[]")
                if not clean:
                    continue
                if clean in noisy and tok.startswith("["):
                    try:
                        proto_file.write_text(f"-{clean}\n", encoding="utf-8")
                    except OSError:
                        pass
                elif clean not in noisy and not tok.startswith("["):
                    try:
                        proto_file.write_text(f"+{clean}\n", encoding="utf-8")
                    except OSError:
                        pass
            after = proto_file.read_text(encoding="utf-8", errors="ignore")
            enabled.extend(t.strip("[]") for t in after.split() if t.startswith("["))
        except OSError:
            pass
    return enabled


def _find_ir_event_devices(mode: str) -> List[Path]:
    """Find `/dev/input/event*` nodes matching FLIRC or GPIO IR."""
    devices: List[Path] = []
    sys_input = Path("/sys/class/input")
    if not sys_input.exists():
        return devices
    for entry in sorted(sys_input.glob("event*")):
        name_file = entry / "device" / "name"
        dev_node = Path("/dev/input") / entry.name
        if not dev_node.exists():
            continue
        try:
            name = name_file.read_text(encoding="utf-8", errors="ignore").strip().lower()
        except Exception:
            continue
        if mode == "flirc" and "flirc" in name:
            devices.append(dev_node)
        elif mode == "gpio" and ("gpio" in name or "lirc" in name or re.search(r"\bir\b", name)):
            devices.append(dev_node)
    return devices


class IRManager:
    """Manages FLIRC USB learning via `flirc_util`, GPIO IR dtoverlay, and key event listening."""

    def __init__(self) -> None:
        self._learning_action: Optional[str] = None
        self._stop_event = threading.Event()
        self._listener_thread: Optional[threading.Thread] = None
        self._activity_lock = threading.Lock()
        self._activity_log: deque[str] = deque(maxlen=40)

    def _record_activity(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        with self._activity_lock:
            self._activity_log.append(f"[{ts}] {msg}")

    def get_activity_log(self) -> List[str]:
        with self._activity_lock:
            return list(self._activity_log)

    def start_listener(
        self,
        loop: asyncio.AbstractEventLoop,
        get_cfg: Callable[[], object],
        on_action: Callable[[str], Awaitable[None]],
    ) -> None:
        if self._listener_thread and self._listener_thread.is_alive():
            return
        self._stop_event.clear()
        self._listener_thread = threading.Thread(
            target=self._listen_loop,
            args=(loop, get_cfg, on_action),
            daemon=True,
        )
        self._listener_thread.start()

    def stop_listener(self) -> None:
        self._stop_event.set()

    def _listen_loop(
        self,
        loop: asyncio.AbstractEventLoop,
        get_cfg: Callable[[], object],
        on_action: Callable[[str], Awaitable[None]],
    ) -> None:
        fds: Dict[int, Path] = {}
        active_mode: Optional[str] = None
        last_action_ts: float = 0.0

        def _close_all() -> None:
            for fd in list(fds):
                try:
                    os.close(fd)
                except OSError:
                    pass
            fds.clear()

        while not self._stop_event.is_set():
            try:
                cfg = get_cfg()
                ir_cfg = getattr(cfg, "ir", None)
                mode = getattr(ir_cfg, "mode", "disabled")
                gpio_keymap = getattr(ir_cfg, "gpio_keymap", {}) or {}
            except Exception:
                mode = "disabled"
                gpio_keymap = {}
            scan_to_action = {int(v): k for k, v in gpio_keymap.items()} if mode == "gpio" else {}

            if mode == "disabled" or self._learning_action is not None:
                _close_all()
                active_mode = None
                time.sleep(0.5)
                continue

            if mode != active_mode or not fds:
                _close_all()
                active_mode = mode
                if mode == "gpio":
                    protos = _enable_gpio_ir_protocols()
                    if protos:
                        self._record_activity(f"GPIO IR protocols active: {', '.join(protos)}")
                for dev_path in _find_ir_event_devices(mode):
                    try:
                        fd = os.open(str(dev_path), os.O_RDONLY | os.O_NONBLOCK)
                        fds[fd] = dev_path
                        logger.info("Listening for IR key events on %s (%s)", dev_path, mode)
                        self._record_activity(f"Listening on {dev_path} ({mode})")
                    except OSError as exc:
                        logger.debug("Could not open IR input %s: %s", dev_path, exc)
                if not fds:
                    time.sleep(2.0)
                    continue

            try:
                readable, _, _ = select.select(list(fds.keys()), [], [], 1.0)
            except (OSError, ValueError):
                _close_all()
                continue

            for fd in readable:
                try:
                    data = os.read(fd, _INPUT_EVENT_SIZE * 16)
                except BlockingIOError:
                    continue
                except OSError:
                    _close_all()
                    break

                offset = 0
                while offset + _INPUT_EVENT_SIZE <= len(data):
                    _, _, ev_type, code, value = struct.unpack_from(
                        _INPUT_EVENT_FMT, data, offset
                    )
                    offset += _INPUT_EVENT_SIZE
                    action: Optional[str] = None
                    if ev_type == _EV_MSC and code == _MSC_SCAN and mode == "gpio":
                        scancode = int(value)
                        if scancode in (0x7FFFFFFF, 0xFFFFFFFF, -1):
                            continue
                        action = scan_to_action.get(scancode)
                        if not action:
                            self._record_activity(
                                f"GPIO unmapped IR scancode {hex(scancode)} (ignored)"
                            )
                            continue
                    elif ev_type == _EV_KEY and value == 1:
                        if code == _KEY_VOLUMEUP:
                            action = "vol_up"
                        elif code == _KEY_VOLUMEDOWN:
                            action = "vol_down"
                        elif code == _KEY_MUTE:
                            action = "mute"
                    if not action:
                        continue
                    now = time.monotonic()
                    if now - last_action_ts < 0.15:
                        continue
                    last_action_ts = now
                    logger.info(
                        "IR event (%s): type=%s code=%s val=%s -> %s",
                        fds.get(fd),
                        ev_type,
                        code,
                        value,
                        action,
                    )
                    self._record_activity(
                        f"{mode.upper()} IR -> {action}"
                        + (f" ({hex(int(value))})" if ev_type == _EV_MSC else f" (key={code})")
                    )
                    asyncio.run_coroutine_threadsafe(on_action(action), loop)

        _close_all()

    def get_gpio_status(self, ir_cfg: IRConfig) -> Dict[str, object]:
        devices = [str(p) for p in _find_ir_event_devices("gpio")]
        return {
            "connected": bool(devices),
            "devices": devices,
            "gpio_pin": ir_cfg.gpio_pin,
            "keymap": {k: hex(v) for k, v in (ir_cfg.gpio_keymap or {}).items()},
            "learning": self._learning_action,
        }

    def learn_gpio_command(
        self,
        action: str,
        existing_keymap: Optional[Dict[str, int]] = None,
        timeout: float = 15.0,
    ) -> Dict[str, object]:
        """Capture 2 matching MSC_SCAN frames from GPIO IR, check duplicates, and drain repeats."""
        if action not in FLIRC_ACTION_TO_KEY:
            return {"ok": False, "error": f"Unsupported IR action: {action}"}
        if self._learning_action is not None:
            return {"ok": False, "error": f"Already learning '{self._learning_action}'; please wait."}
        protos = _enable_gpio_ir_protocols()
        devices = _find_ir_event_devices("gpio")
        if not devices:
            msg = "No GPIO IR receiver found (/dev/input/event*). Save GPIO mode and reboot first."
            self._record_activity(f"GPIO learn '{action}' failed: {msg}")
            return {"ok": False, "action": action, "error": msg}
        self._learning_action = action
        self._record_activity(
            f"Learning '{action}' (GPIO, protocols: {', '.join(protos) or 'default'}) — press button twice (or hold briefly)…"
        )
        fds: Dict[int, Path] = {}
        try:
            time.sleep(0.15)  # allow _listen_loop to release fds
            for dev_path in devices:
                try:
                    fd = os.open(str(dev_path), os.O_RDONLY | os.O_NONBLOCK)
                    fds[fd] = dev_path
                except OSError:
                    pass
            if not fds:
                msg = f"Unable to open GPIO IR input device ({devices[0]})."
                self._record_activity(f"GPIO learn '{action}' failed: {msg}")
                return {"ok": False, "action": action, "error": msg}

            def _drain_fds() -> None:
                for d_fd in list(fds):
                    try:
                        while os.read(d_fd, _INPUT_EVENT_SIZE * 16):
                            pass
                    except (BlockingIOError, OSError):
                        pass

            _drain_fds()
            first_code: Optional[int] = None
            first_ts: float = 0.0
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                remaining = max(0.05, min(0.5, deadline - time.monotonic()))
                readable, _, _ = select.select(list(fds.keys()), [], [], remaining)
                for fd in readable:
                    try:
                        data = os.read(fd, _INPUT_EVENT_SIZE * 16)
                    except (BlockingIOError, OSError):
                        continue
                    offset = 0
                    while offset + _INPUT_EVENT_SIZE <= len(data):
                        _, _, ev_type, code, value = struct.unpack_from(
                            _INPUT_EVENT_FMT, data, offset
                        )
                        offset += _INPUT_EVENT_SIZE
                        if ev_type != _EV_MSC or code != _MSC_SCAN:
                            continue
                        scancode = int(value)
                        if scancode in (0x7FFFFFFF, 0xFFFFFFFF, -1):
                            continue
                        now = time.monotonic()
                        if first_code == scancode and (now - first_ts) <= 2.5:
                            dup_action = next(
                                (
                                    k
                                    for k, v in (existing_keymap or {}).items()
                                    if k != action and int(v) == scancode
                                ),
                                None,
                            )
                            time.sleep(0.25)
                            _drain_fds()
                            if dup_action:
                                err = (
                                    f"Button ({hex(scancode)}) is already assigned to '{dup_action}'. "
                                    "Press a different button or Clear Learned Keys."
                                )
                                self._record_activity(f"GPIO learn '{action}' rejected: {err}")
                                return {"ok": False, "action": action, "error": err}
                            if existing_keymap is not None:
                                existing_keymap[action] = scancode
                            self._record_activity(
                                f"Confirmed '{action}' -> {hex(scancode)} (trailing repeats drained)"
                            )
                            return {
                                "ok": True,
                                "action": action,
                                "scancode": scancode,
                                "hex": hex(scancode),
                            }
                        first_code = scancode
                        first_ts = now
                        self._record_activity(
                            f"Captured frame 1/2 for '{action}': {hex(scancode)} — confirm with repeat/tap"
                        )
            self._record_activity(f"GPIO learn '{action}' timed out (15s).")
            return {
                "ok": False,
                "action": action,
                "error": "Timed out waiting for 2 matching IR frames (15s).",
            }
        finally:
            for fd in list(fds):
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._learning_action = None

    def get_flirc_status(self) -> Dict[str, object]:
        try:
            res = subprocess.run(
                ["flirc_util", "settings"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            out = (res.stdout or res.stderr).strip()
            if "could not find command" in out.lower():
                res = subprocess.run(
                    ["flirc_util", "version"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                out = (res.stdout or res.stderr).strip()
            connected = (
                res.returncode == 0
                and "disconnected" not in out.lower()
                and bool(out)
            )
            summary = out
            if connected:
                fw_m = re.search(r"FW Version:\s*([^\r\n]+)", out)
                sku_m = re.search(r"SKU:\s*([^\r\n]+)", out)
                parts = [m.group(1).strip() for m in (sku_m, fw_m) if m]
                if parts:
                    summary = ", ".join(parts)
                else:
                    summary = out.splitlines()[0].strip()[:80]
            return {
                "installed": True,
                "connected": connected,
                "raw": summary,
                "learning": self._learning_action,
            }
        except FileNotFoundError:
            return {
                "installed": False,
                "connected": False,
                "raw": "flirc_util not installed",
                "learning": self._learning_action,
            }
        except Exception as exc:
            return {
                "installed": True,
                "connected": False,
                "raw": str(exc),
                "learning": self._learning_action,
            }

    def learn_flirc_command(self, action: str) -> Dict[str, object]:
        """Trigger `flirc_util record <key>` for Web GUI IR learning."""
        if action not in FLIRC_ACTION_TO_KEY:
            return {"ok": False, "error": f"Unsupported IR action: {action}"}
        flirc_key = FLIRC_ACTION_TO_KEY[action]
        self._learning_action = action
        try:
            res = subprocess.run(
                ["flirc_util", "record", flirc_key],
                capture_output=True,
                text=True,
                timeout=15,
            )
            ok = res.returncode == 0
            return {
                "ok": ok,
                "action": action,
                "output": (res.stdout or res.stderr).strip(),
            }
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "action": action,
                "error": "Timed out waiting for IR remote button press (15s).",
            }
        except FileNotFoundError:
            return {
                "ok": False,
                "action": action,
                "error": "flirc_util is not installed on this host.",
            }
        finally:
            self._learning_action = None

    def clear_flirc(self) -> Dict[str, object]:
        try:
            res = subprocess.run(
                ["flirc_util", "format"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return {"ok": res.returncode == 0, "output": (res.stdout or res.stderr).strip()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def apply_gpio_overlay(self, cfg: IRConfig) -> Dict[str, object]:
        """Configure Raspberry Pi `dtoverlay=gpio-ir,gpio_pin=X` when GPIO IR is selected."""
        if cfg.mode != "gpio":
            return {"ok": True, "reboot_required": False}

        boot_file = next((p for p in BOOT_CONFIG_PATHS if p.exists()), None)
        if not boot_file:
            return {
                "ok": False,
                "reboot_required": False,
                "message": "Raspberry Pi boot config.txt not found.",
            }

        try:
            lines = boot_file.read_text(encoding="utf-8").splitlines()
            filtered = [
                ln for ln in lines if not ln.strip().startswith("dtoverlay=gpio-ir")
            ]
            new_line = f"dtoverlay=gpio-ir,gpio_pin={cfg.gpio_pin}"
            if new_line in lines:
                return {"ok": True, "reboot_required": False}
            filtered.append(new_line)
            boot_file.write_text("\n".join(filtered) + "\n", encoding="utf-8")
            return {
                "ok": True,
                "reboot_required": True,
                "message": f"Updated {boot_file} with {new_line}. Reboot required.",
            }
        except PermissionError:
            return {
                "ok": False,
                "reboot_required": False,
                "message": "Insufficient permissions to modify boot config.txt.",
            }
