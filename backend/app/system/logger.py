"""Bounded 500-entry rolling log file handler for Hydra Sound."""

from __future__ import annotations

import logging
import os
import threading
from collections import deque
from pathlib import Path
from typing import List, Optional


def default_log_path() -> Path:
    cfg_env = os.environ.get("HYDRA_CONFIG_PATH")
    if cfg_env:
        return Path(cfg_env).parent / "hydrasound.log"
    etc_dir = Path("/etc/hydrasound")
    if etc_dir.exists() and os.access(etc_dir, os.W_OK):
        return etc_dir / "hydrasound.log"
    return Path(__file__).resolve().parents[2] / "data" / "hydrasound.log"


class Rolling500FileHandler(logging.Handler):
    """Keeps at most `max_entries` (default 500) log lines and overwrites the file."""

    def __init__(
        self,
        log_path: Optional[Path] = None,
        max_entries: int = 500,
        enabled: bool = False,
    ) -> None:
        super().__init__()
        self.log_path = log_path or default_log_path()
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._enabled = enabled
        self._lines: deque[str] = deque(maxlen=max_entries)
        if self._enabled:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._load_existing()

    def is_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = enabled
            if enabled:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                if not self._lines:
                    self._load_existing()

    def _load_existing(self) -> None:
        if self.log_path.exists():
            try:
                existing = self.log_path.read_text(encoding="utf-8").splitlines()
                self._lines.extend(existing[-self.max_entries :])
            except Exception:
                pass

    def emit(self, record: logging.LogRecord) -> None:
        try:
            with self._lock:
                if not self._enabled:
                    return
                msg = self.format(record)
                self._lines.append(msg)
                self._write_locked()
        except Exception:
            self.handleError(record)

    def _write_locked(self) -> None:
        content = "\n".join(self._lines) + ("\n" if self._lines else "")
        self.log_path.write_text(content, encoding="utf-8")

    def get_lines(self) -> List[str]:
        with self._lock:
            return list(self._lines)

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()
            self._write_locked()
