"""Persistent state and process-identity helpers.

Each service has a JSON state file under the state dir. Writes are atomic
(tmp file + rename). Process identity is verified with the /proc starttime
tick so a recycled PID is never mistaken for a managed process.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path) -> dict | None:
    try:
        with path.open() as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def proc_starttime(pid: int) -> int | None:
    """Start time (clock ticks since boot) of a process, from /proc.

    Returns None when /proc is unavailable or the process is gone.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    # comm (field 2) may contain spaces/parens; fields resume after last ')'.
    rparen = data.rfind(b")")
    if rparen < 0:
        return None
    fields = data[rparen + 2 :].split()
    # field 22 (starttime) is index 19 after dropping pid and comm.
    if len(fields) <= 19:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def pid_matches(pid: int, starttime: int | None) -> bool:
    """True if `pid` is alive and (when known) has the recorded starttime."""
    if not pid_alive(pid):
        return False
    if starttime is None:
        # No identity token recorded; cannot prove it is ours.
        return False
    current = proc_starttime(pid)
    if current is None:
        # /proc unavailable: fall back to liveness only.
        return True
    return current == starttime


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()) + (
        f".{int((time.time() % 1) * 1000):03d}"
    )
