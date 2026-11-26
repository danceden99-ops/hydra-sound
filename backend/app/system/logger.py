"""Bounded rolling log file handler for Hydra Sound with SD-card-friendly batching."""

from __future__ import annotations

import logging
import os
import threading
import time
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
    """Keeps at most `max_entries` (default 2000) log lines using batched appends and periodic compaction."""

    def __init__(
        self,
        log_path: Optional[Path] = None,
        max_entries: int = 2000,
        enabled: bool = False,
        flush_batch: int = 25,
        flush_interval: float = 2.0,
        compact_slack: int = 500,
    ) -> None:
        super().__init__()
        self.log_path = log_path or default_log_path()
        self.max_entries = max_entries
        self._flush_batch = max(1, flush_batch)
        self._flush_interval = max(0.1, flush_interval)
        self._compact_slack = max(0, compact_slack)
        self._lock = threading.Lock()
        self._enabled = enabled
        self._lines: deque[str] = deque(maxlen=max_entries)
        self._pending: List[str] = []
        self._written_lines = 0
        self._last_flush = time.monotonic()
        if self._enabled:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._load_existing()

    def is_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            if not enabled and self._enabled:
                self._flush_locked()
            self._enabled = enabled
            if enabled:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                if not self._lines:
                    self._load_existing()

    def _load_existing(self) -> None:
        if self.log_path.exists():
            try:
                existing = self.log_path.read_text(encoding="utf-8").splitlines()
                self._written_lines = len(existing)
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
                self._pending.append(msg)
                now = time.monotonic()
                if (
                    len(self._pending) >= self._flush_batch
                    or record.levelno >= logging.ERROR
                    or (now - self._last_flush) >= self._flush_interval
                ):
                    self._flush_locked()
        except Exception:
            self.handleError(record)

    def _flush_locked(self) -> None:
        if not self._pending:
            return
        if self._written_lines + len(self._pending) > self.max_entries + self._compact_slack:
            self._write_locked()
        else:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write("\n".join(self._pending) + "\n")
            self._written_lines += len(self._pending)
            self._pending.clear()
        self._last_flush = time.monotonic()

    def _write_locked(self) -> None:
        content = "\n".join(self._lines) + ("\n" if self._lines else "")
        self.log_path.write_text(content, encoding="utf-8")
        self._pending.clear()
        self._written_lines = len(self._lines)

    def flush(self) -> None:
        with self._lock:
            if self._enabled:
                self._flush_locked()

    def compact(self) -> None:
        """Trim the on-disk log file down to `max_entries`."""
        with self._lock:
            self._pending.clear()
            self._write_locked()

    def close(self) -> None:
        try:
            self.flush()
        finally:
            super().close()

    def get_lines(self) -> List[str]:
        with self._lock:
            return list(self._lines)

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()
            self._pending.clear()
            self._write_locked()
