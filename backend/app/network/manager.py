"""Network configuration manager (DHCP / Static IP via NetworkManager & Avahi mDNS)."""

from __future__ import annotations

import logging
import socket
import subprocess
from typing import Dict

from app.config import NetworkConfig

logger = logging.getLogger(__name__)


class NetworkManager:
    """Manages IPv4 DHCP/Static configuration and `hydrasound.local` mDNS."""

    def get_status(self, cfg: NetworkConfig) -> Dict[str, object]:
        ip_addr = self._get_local_ip()
        return {
            "hostname": cfg.hostname,
            "mdns_url": f"http://{cfg.hostname}.local",
            "ip_address": ip_addr,
            "mode": cfg.mode,
            "interface": cfg.interface,
        }

    @staticmethod
    def _get_local_ip() -> str:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"

    def apply(self, cfg: NetworkConfig) -> Dict[str, object]:
        """Apply hostname + DHCP or Static IP using `hostnamectl` and `nmcli`."""
        errors = []
        try:
            subprocess.run(
                ["hostnamectl", "set-hostname", cfg.hostname],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception as exc:
            errors.append(f"hostname: {exc}")

        try:
            con_name = self._find_nm_connection(cfg.interface)
            if con_name:
                if cfg.mode == "dhcp":
                    subprocess.run(
                        [
                            "nmcli",
                            "connection",
                            "modify",
                            con_name,
                            "ipv4.method",
                            "auto",
                            "ipv4.addresses",
                            "",
                            "ipv4.gateway",
                            "",
                            "ipv4.dns",
                            "",
                        ],
                        capture_output=True,
                        timeout=5,
                    )
                else:
                    addr = f"{cfg.static_ip}/{cfg.subnet_mask}"
                    cmd = [
                        "nmcli",
                        "connection",
                        "modify",
                        con_name,
                        "ipv4.method",
                        "manual",
                        "ipv4.addresses",
                        addr,
                    ]
                    if cfg.gateway:
                        cmd.extend(["ipv4.gateway", cfg.gateway])
                    if cfg.dns:
                        cmd.extend(["ipv4.dns", cfg.dns])
                    subprocess.run(cmd, capture_output=True, timeout=5)

                subprocess.run(
                    ["nmcli", "connection", "up", con_name],
                    capture_output=True,
                    timeout=10,
                )
        except Exception as exc:
            errors.append(f"nmcli: {exc}")

        return {"ok": len(errors) == 0, "errors": errors}

    def scan_wifi(self) -> list[Dict[str, object]]:
        """Scan nearby Wi-Fi networks via `nmcli`."""
        try:
            res = subprocess.run(
                [
                    "nmcli",
                    "-t",
                    "-f",
                    "SSID,SIGNAL,SECURITY",
                    "dev",
                    "wifi",
                    "list",
                    "--rescan",
                    "yes",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if res.returncode != 0:
                return []
            seen: set[str] = set()
            networks: list[Dict[str, object]] = []
            for line in res.stdout.splitlines():
                parts = line.split(":")
                if len(parts) < 3:
                    continue
                ssid = parts[0].strip()
                if not ssid or ssid in seen:
                    continue
                seen.add(ssid)
                networks.append(
                    {
                        "ssid": ssid,
                        "signal": int(parts[1]) if parts[1].isdigit() else 0,
                        "security": parts[2].strip() or "Open",
                    }
                )
            return sorted(networks, key=lambda n: int(n["signal"]), reverse=True)
        except Exception:
            return []

    def connect_wifi(self, ssid: str, password: str = "") -> Dict[str, object]:
        """Connect `wlan0` to a Wi-Fi SSID using NetworkManager."""
        if not ssid.strip():
            return {"ok": False, "error": "Wi-Fi SSID cannot be empty."}
        cmd = ["nmcli", "dev", "wifi", "connect", ssid.strip()]
        if password:
            cmd.extend(["password", password])
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            return {
                "ok": res.returncode == 0,
                "output": (res.stdout or res.stderr).strip(),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _find_nm_connection(interface: str) -> str | None:
        try:
            res = subprocess.run(
                ["nmcli", "-t", "-f", "NAME,DEVICE", "connection", "show", "--active"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            for line in res.stdout.splitlines():
                parts = line.split(":")
                if len(parts) == 2 and parts[1] == interface:
                    return parts[0]
        except Exception:
            pass
        return None
