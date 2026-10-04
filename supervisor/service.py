"""Per-service process management: state machine, restart policy,
exponential backoff, graceful shutdown, log capture and recovery.

State machine:

    STOPPED -> STARTING -> RUNNING -> EXITED          (policy never, clean exit)
                             |-----> FAILED           (never/on-failure, bad exit)
                             |-----> BACKOFF -> STARTING (restart policies)
                             |-----> STOPPING -> STOPPED  (graceful stop,
                                                           SIGKILL on timeout)
"""

from __future__ import annotations

import enum
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from .config import ServiceConfig
from .logrotate import RotatingLogWriter
from .state import atomic_write_json, now_iso, pid_matches, proc_starttime, read_json


class ServiceStatus(str, enum.Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    BACKOFF = "backoff"
    STOPPING = "stopping"
    EXITED = "exited"
    FAILED = "failed"


class Service:
    def __init__(self, cfg: ServiceConfig, state_dir: Path, log_dir: Path):
        self.cfg = cfg
        self.state_dir = Path(state_dir)
        self.log_dir = Path(log_dir)
        self.state_path = self.state_dir / f"{cfg.name}.state.json"
        self.log_path = self.log_dir / f"{cfg.name}.log"

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._adopted_pid: int | None = None
        self._monitor_thread: threading.Thread | None = None
        self._reader_threads: list[threading.Thread] = []
        self._log: RotatingLogWriter | None = None

        self.status = ServiceStatus.STOPPED
        self.pid: int | None = None
        self.pgid: int | None = None
        self.proc_starttime: int | None = None
        self.started_at: float | None = None
        self.exited_at: float | None = None
        self.exit_code: int | None = None
        self.consecutive_restarts = 0
        self.restarts: list[dict] = []

    # ------------------------------------------------------------------ state

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "name": self.cfg.name,
                "status": self.status.value,
                "pid": self.pid,
                "pgid": self.pgid,
                "proc_starttime": self.proc_starttime,
                "started_at": self.started_at,
                "started_at_iso": now_iso() if self.started_at else None,
                "exited_at": self.exited_at,
                "exit_code": self.exit_code,
                "consecutive_restarts": self.consecutive_restarts,
                "restart_count": len(self.restarts),
                "restarts": list(self.restarts),
                "command": self.cfg.command,
                "restart_policy": self.cfg.restart,
                "log": str(self.log_path),
            }

    def _save(self) -> None:
        atomic_write_json(self.state_path, self.snapshot())

    def _set_status(self, status: ServiceStatus) -> None:
        with self._lock:
            self.status = status
        self._save()

    # ------------------------------------------------------------------- logs

    def _log_writer(self) -> RotatingLogWriter:
        with self._lock:
            if self._log is None:
                self._log = RotatingLogWriter(
                    self.log_path, self.cfg.log_max_bytes, self.cfg.log_backups
                )
            return self._log

    def _pump_stream(self, stream, stream_name: str) -> None:
        writer = self._log_writer()
        try:
            while True:
                line = stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\n")
                record = f"[{now_iso()} {stream_name}] {text}\n".encode("utf-8")
                writer.write_record(record)
        except (ValueError, OSError):
            pass

    # ----------------------------------------------------------------- spawn

    def _spawn(self) -> subprocess.Popen:
        env = dict(os.environ)
        env.update(self.cfg.env)
        proc = subprocess.Popen(
            self.cfg.command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # own process group: pgid == pid
            cwd=self.cfg.cwd,
            env=env,
        )
        with self._lock:
            self._proc = proc
            self.pid = proc.pid
            self.pgid = proc.pid
            self.proc_starttime = proc_starttime(proc.pid)
            self.started_at = time.time()
            self.exited_at = None
            self.exit_code = None
        for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
            t = threading.Thread(
                target=self._pump_stream, args=(stream, name), daemon=True
            )
            t.start()
            self._reader_threads.append(t)
        self._set_status(ServiceStatus.RUNNING)
        return proc

    # ---------------------------------------------------------------- monitor

    def start(self) -> None:
        with self._lock:
            if self.status in (
                ServiceStatus.RUNNING,
                ServiceStatus.STARTING,
                ServiceStatus.BACKOFF,
            ):
                return
            self._stop_event.clear()
            self.status = ServiceStatus.STARTING
            self._save()
            self._monitor_thread = threading.Thread(
                target=self._monitor, name=f"sv-{self.cfg.name}", daemon=True
            )
            self._monitor_thread.start()

    def _should_restart(self, exit_code: int | None) -> bool:
        policy = self.cfg.restart
        if policy == "always":
            return True
        if policy == "on-failure":
            return exit_code is not None and exit_code != 0
        return False

    def _backoff_delay(self) -> float:
        delay = self.cfg.backoff_initial * (
            self.cfg.backoff_factor ** self.consecutive_restarts
        )
        return min(delay, self.cfg.backoff_max)

    def _after_exit(self, exit_code: int | None, run_duration: float | None) -> bool:
        """Record the exit and decide whether to restart. Returns True to loop."""
        with self._lock:
            self.exited_at = time.time()
            self.exit_code = exit_code
            if run_duration is None or run_duration >= self.cfg.backoff_reset_after:
                self.consecutive_restarts = 0

        if self._stop_event.is_set():
            self._set_status(ServiceStatus.STOPPED)
            return False
        if not self._should_restart(exit_code):
            self._set_status(
                ServiceStatus.EXITED if exit_code in (0, None) else ServiceStatus.FAILED
            )
            return False
        if self.consecutive_restarts >= self.cfg.max_restarts:
            self._set_status(ServiceStatus.FAILED)
            return False

        delay = self._backoff_delay()
        with self._lock:
            self.consecutive_restarts += 1
            self.restarts.append(
                {
                    "seq": len(self.restarts) + 1,
                    "time": time.time(),
                    "time_iso": now_iso(),
                    "delay": delay,
                    "last_exit_code": exit_code,
                }
            )
        self._set_status(ServiceStatus.BACKOFF)
        if self._stop_event.wait(delay):
            self._set_status(ServiceStatus.STOPPED)
            return False
        return True

    def _monitor(self) -> None:
        try:
            while True:
                proc = self._spawn()
                started = time.monotonic()
                exit_code = proc.wait()
                duration = time.monotonic() - started
                for t in self._reader_threads:
                    t.join(timeout=5)
                self._reader_threads = []
                if not self._after_exit(exit_code, duration):
                    return
        finally:
            with self._lock:
                self._proc = None
                if self.status == ServiceStatus.RUNNING:
                    self.status = ServiceStatus.STOPPED

    def _adopt_monitor(self, pid: int, starttime: int | None) -> None:
        """Monitor an adopted process we did not spawn (no pipes, no wait())."""
        try:
            while pid_matches(pid, starttime):
                if self._stop_event.wait(0.1):
                    return
            if not self._stop_event.is_set():
                # Unknown exit code for an adopted process.
                self._after_exit(None, None)
        finally:
            with self._lock:
                self._adopted_pid = None

    # ------------------------------------------------------------------- stop

    def _signal_group(self, sig: int) -> None:
        try:
            if self.pgid:
                os.killpg(self.pgid, sig)
            elif self.pid:
                os.kill(self.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def stop(self, timeout: float | None = None) -> None:
        """Graceful stop: configured signal first, SIGKILL after timeout."""
        timeout = self.cfg.stop_timeout if timeout is None else timeout
        self._stop_event.set()
        with self._lock:
            proc = self._proc
            adopted = self._adopted_pid
        if proc is not None and proc.poll() is None:
            self._set_status(ServiceStatus.STOPPING)
            self._signal_group(getattr(signal, f"SIG{self.cfg.stop_signal}"))
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._signal_group(signal.SIGKILL)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        elif adopted is not None and pid_matches(adopted, self.proc_starttime):
            self._set_status(ServiceStatus.STOPPING)
            self._signal_group(getattr(signal, f"SIG{self.cfg.stop_signal}"))
            deadline = time.monotonic() + timeout
            while pid_matches(adopted, self.proc_starttime):
                if time.monotonic() >= deadline:
                    try:
                        os.kill(adopted, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                    break
                time.sleep(0.05)
        thread = self._monitor_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout + 10)
        with self._lock:
            if self.status not in (ServiceStatus.STOPPED,):
                self.status = ServiceStatus.STOPPED
        self._save()

    def close(self) -> None:
        if self._log is not None:
            self._log.close()
            self._log = None

    # ---------------------------------------------------------------- recover

    def recover(self) -> str:
        """Reconcile persisted state with reality after a supervisor restart.

        Returns "adopted", "stale" or "fresh".
        """
        data = read_json(self.state_path)
        if not data:
            return "fresh"
        with self._lock:
            self.consecutive_restarts = data.get("consecutive_restarts", 0)
            self.restarts = data.get("restarts", [])
        status = data.get("status")
        pid = data.get("pid")
        starttime = data.get("proc_starttime")
        live_states = {
            ServiceStatus.RUNNING.value,
            ServiceStatus.STARTING.value,
            ServiceStatus.BACKOFF.value,
            ServiceStatus.STOPPING.value,
        }
        if status in live_states and pid and pid_matches(pid, starttime):
            with self._lock:
                self.pid = pid
                self.pgid = data.get("pgid") or pid
                self.proc_starttime = starttime
                self.started_at = data.get("started_at")
                self._adopted_pid = pid
                self._stop_event.clear()
                self.status = ServiceStatus.RUNNING
            self._save()
            self._monitor_thread = threading.Thread(
                target=self._adopt_monitor,
                args=(pid, starttime),
                name=f"sv-{self.cfg.name}-adopt",
                daemon=True,
            )
            self._monitor_thread.start()
            return "adopted"
        # The recorded PID is dead or was recycled by an unrelated process:
        # it is no longer a managed process.
        with self._lock:
            self.status = ServiceStatus.STOPPED
            self.pid = None
            self.pgid = None
            self.proc_starttime = None
        self._save()
        return "stale"
