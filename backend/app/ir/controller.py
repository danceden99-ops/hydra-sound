"""IR Receiver Manager for FLIRC USB (with Web GUI learning) and GPIO IR."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Dict, Optional

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


class IRManager:
    """Manages FLIRC USB learning via `flirc_util` and GPIO IR dtoverlay."""

    def __init__(self) -> None:
        self._learning_action: Optional[str] = None

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
            return {
                "installed": True,
                "connected": connected,
                "raw": out,
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
