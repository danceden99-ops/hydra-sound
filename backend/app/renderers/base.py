"""Base interface for modular streaming renderer adapters."""

from __future__ import annotations

import logging
import subprocess
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from app.config import HydraConfig

logger = logging.getLogger(__name__)


@dataclass
class RendererMetadata:
    id: str
    display_name: str
    binary_name: str
    github_repo: str
    update_script: str
    quality_note: str


class RendererAdapter(ABC):
    """Abstract base class so each streaming protocol can be updated independently."""

    meta: RendererMetadata

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self.connection_status: str = "stopped"  # stopped | advertising | connected | error
        self.connection_detail: str = "Not running"

    @abstractmethod
    def is_enabled(self, cfg: HydraConfig) -> bool:
        """Return whether this renderer is enabled in config."""

    @abstractmethod
    def build_command(self, cfg: HydraConfig, alsa_device: str) -> list[str]:
        """Build the CLI command to launch this renderer targeting `alsa_device`."""

    def start(self, cfg: HydraConfig, alsa_device: str) -> bool:
        self.stop()
        if not self.is_enabled(cfg):
            self.connection_status = "disabled"
            self.connection_detail = "Disabled"
            return False
        cmd = self.build_command(cfg, alsa_device)
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self.connection_status = "advertising"
            self.connection_detail = "Discoverable on LAN"
            threading.Thread(
                target=self._drain_stderr,
                args=(self._proc,),
                daemon=True,
            ).start()
            logger.info("Started renderer %s on %s", self.meta.id, alsa_device)
            return True
        except FileNotFoundError:
            self.connection_status = "not_installed"
            self.connection_detail = f"{self.meta.binary_name} not installed"
            logger.warning(
                "Binary '%s' not installed for %s; running in standby/mock mode.",
                self.meta.binary_name,
                self.meta.id,
            )
            return False

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        stream = proc.stdout or proc.stderr
        if not stream:
            return
        try:
            for line in stream:
                clean = line.strip()
                if clean:
                    self._update_connection_state(clean)
                    logger.warning("[%s] %s", self.meta.id, clean)
        except Exception:
            pass

    def _update_connection_state(self, line: str) -> None:
        """Infer live login/pairing/error state from renderer output."""
        low = line.lower()
        if "authenticated as" in low or "lifecycle -> connected" in low or "taking over" in low:
            self.connection_status = "connected"
            self.connection_detail = "Connected & authenticated"
        elif "serving /streamcore" in low or "ready for rendering" in low:
            if self.connection_status != "connected":
                self.connection_status = "advertising"
                self.connection_detail = "Discoverable on LAN"
        elif (
            " ERROR " in line
            or "ERROR:" in line
            or "panicked at" in low
            or "fatal" in low
        ) and "mdns_sd::service_daemon" not in low:
            self.connection_status = "error"
            self.connection_detail = line[:120]

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        self.connection_status = "stopped"
        self.connection_detail = "Stopped"

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def set_client_volume(self, volume_percent: int, muted: bool) -> None:
        """Push volume update back to the active streaming client if supported."""
        return None

    def get_installed_version(self) -> Optional[str]:
        try:
            res = subprocess.run(
                [self.meta.binary_name, "--version"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            output = (res.stdout or res.stderr).strip()
            return output.splitlines()[0] if output else None
        except Exception:
            return None
