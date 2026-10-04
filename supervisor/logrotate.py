"""Size-based rotating log writer.

Rotation happens only between complete records (one ``write_record`` call
= one complete line), and all writes are serialized under a lock, so a
record that has been fully written is never torn or lost by rotation.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path


class RotatingLogWriter:
    def __init__(self, path: str | Path, max_bytes: int, backups: int):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.backups = backups
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._closed = False
        self._size = self.path.stat().st_size if self.path.exists() else 0
        self._fh = open(self.path, "ab", buffering=0)

    def write_record(self, data: bytes) -> None:
        """Atomically append one complete record, rotating first if needed."""
        if not data:
            return
        with self._lock:
            if self._closed:
                raise ValueError("writer is closed")
            if self._size > 0 and self._size + len(data) > self.max_bytes:
                self._rotate_locked()
            self._fh.write(data)
            self._size += len(data)

    def _rotate_locked(self) -> None:
        self._fh.close()
        if self.backups > 0:
            oldest = self.path.with_name(self.path.name + f".{self.backups}")
            if oldest.exists():
                oldest.unlink()
            for i in range(self.backups - 1, 0, -1):
                src = self.path.with_name(self.path.name + f".{i}")
                dst = self.path.with_name(self.path.name + f".{i + 1}")
                if src.exists():
                    os.replace(src, dst)
            if self.path.exists():
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))
        else:
            if self.path.exists():
                self.path.unlink()
        self._fh = open(self.path, "wb", buffering=0)
        self._size = 0

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._fh.close()

    def __enter__(self) -> "RotatingLogWriter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
