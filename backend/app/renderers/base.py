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

    @abstractmethod
    def is_enabled(self, cfg: HydraConfig) -> bool:
        """Return whether this renderer is enabled in config."""

    @abstractmethod
    def build_command(self, cfg: HydraConfig, alsa_device: str) -> list[str]:
        """Build the CLI command to launch this renderer targeting `alsa_device`."""

    def start(self, cfg: HydraConfig, alsa_device: str) -> bool:
        self.stop()
        if not self.is_enabled(cfg):
            return False
        cmd = self.build_command(cfg, alsa_device)
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            threading.Thread(
                target=self._drain_stderr,
                args=(self._proc,),
                daemon=True,
            ).start()
            logger.info("Started renderer %s on %s", self.meta.id, alsa_device)
            return True
        except FileNotFoundError:
            logger.warning(
                "Binary '%s' not installed for %s; running in standby/mock mode.",
                self.meta.binary_name,
                self.meta.id,
            )
            return False

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        if not proc.stderr:
            return
        try:
            for line in proc.stderr:
                clean = line.strip()
                if clean:
                    logger.warning("[%s] %s", self.meta.id, clean)
        except Exception:
            pass

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

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
