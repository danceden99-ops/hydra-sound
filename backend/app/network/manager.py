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
        ip_addr, cidr = self._get_local_ip_and_cidr()
        active_iface = self._get_default_interface() or cfg.interface
        live_mode = cfg.mode
        gateway = cfg.gateway
        dns = cfg.dns

        con_name = self._find_nm_connection(active_iface)
        if con_name:
            nm_info = self._get_nm_details(con_name)
            if nm_info.get("method") == "auto":
                live_mode = "dhcp"
            elif nm_info.get("method") == "manual":
                live_mode = "static"
            if nm_info.get("gateway"):
                gateway = nm_info["gateway"]
            if nm_info.get("dns"):
                dns = nm_info["dns"]

        return {
            "hostname": cfg.hostname,
            "mdns_url": f"http://{cfg.hostname}.local",
            "ip_address": ip_addr,
            "cidr": cidr,
            "gateway": gateway,
            "dns": dns,
            "mode": live_mode,
            "interface": active_iface,
        }

    @staticmethod
    def _get_local_ip_and_cidr() -> tuple[str, str]:
        ip_addr = "127.0.0.1"
        cidr = "24"
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                ip_addr = s.getsockname()[0]
        except Exception:
            pass
        try:
            res = subprocess.run(
                ["ip", "-o", "-4", "addr", "show"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            for line in res.stdout.splitlines():
                if f" {ip_addr}/" in line:
                    part = line.split(f" {ip_addr}/", 1)[1].split()[0]
                    if part.isdigit():
                        cidr = part
                    break
        except Exception:
            pass
        return ip_addr, cidr

    @staticmethod
    def _get_default_interface() -> str | None:
        try:
            res = subprocess.run(
                ["ip", "route", "show", "default"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            parts = res.stdout.split()
            if "dev" in parts:
                return parts[parts.index("dev") + 1]
        except Exception:
            pass
        return None

    @staticmethod
    def _get_nm_details(con_name: str) -> Dict[str, str]:
        details: Dict[str, str] = {}
        try:
            res = subprocess.run(
                [
                    "nmcli",
                    "-t",
                    "-f",
                    "ipv4.method,IP4.GATEWAY,IP4.DNS",
                    "connection",
                    "show",
                    con_name,
                ],
                capture_output=True,
                text=True,
                timeout=3,
            )
            for line in res.stdout.splitlines():
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                v = v.strip()
                if not v:
                    continue
                if k == "ipv4.method":
                    details["method"] = v
                elif k.startswith("IP4.GATEWAY"):
                    details["gateway"] = v
                elif k.startswith("IP4.DNS") and "dns" not in details:
                    details["dns"] = v
        except Exception:
            pass
        return details

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
            subprocess.run(
                ["systemctl", "restart", "avahi-daemon"],
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

    def scan_wifi(self, country: str = "") -> list[Dict[str, object]]:
        """Unblock Wi-Fi if needed, ensure radio is on, and scan via `nmcli`."""
        import re
        import time

        if country:
            self.set_wifi_country(country)
        else:
            try:
                subprocess.run(["rfkill", "unblock", "wifi"], capture_output=True, timeout=3)
            except Exception:
                pass
        try:
            subprocess.run(["nmcli", "radio", "wifi", "on"], capture_output=True, timeout=3)
        except Exception:
            pass

        stdout = ""
        for args in (
            ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "--rescan", "yes"],
            ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"],
        ):
            try:
                res = subprocess.run(args, capture_output=True, text=True, timeout=15)
                if res.returncode == 0 and res.stdout.strip():
                    stdout = res.stdout
                    break
                time.sleep(1)
            except Exception:
                pass

        if not stdout:
            return []

        seen: set[str] = set()
        networks: list[Dict[str, object]] = []
        for line in stdout.splitlines():
            parts = re.split(r"(?<!\\):", line)
            if len(parts) < 3:
                continue
            ssid = parts[0].replace("\\:", ":").strip()
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

    def set_wifi_country(self, country_code: str) -> None:
        """Set Wi-Fi regulatory domain and unblock rfkill on Raspberry Pi."""
        cc = country_code.strip().upper()
        if len(cc) != 2 or not cc.isalpha():
            return
        try:
            subprocess.run(["rfkill", "unblock", "wifi"], capture_output=True, timeout=3)
            subprocess.run(
                ["raspi-config", "nonint", "do_wifi_country", cc],
                capture_output=True,
                timeout=5,
            )
            subprocess.run(["iw", "reg", "set", cc], capture_output=True, timeout=3)
        except Exception:
            pass

    def connect_wifi(self, ssid: str, password: str = "", country: str = "") -> Dict[str, object]:
        """Connect `wlan0` to a Wi-Fi SSID using NetworkManager."""
        if country:
            self.set_wifi_country(country)
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
