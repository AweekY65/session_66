"""Top-level supervisor: owns services, metadata and signal handling."""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from pathlib import Path

from .config import SupervisorConfig, load_config
from .service import Service
from .state import atomic_write_json, now_iso, read_json


class Supervisor:
    def __init__(self, config: SupervisorConfig):
        self.config = config
        self.state_dir = Path(config.state_dir)
        self.log_dir = config.log_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.state_dir / "supervisor.json"
        self.services = {
            cfg.name: Service(cfg, self.state_dir, self.log_dir)
            for cfg in config.services
        }
        self._shutdown = threading.Event()

    @classmethod
    def from_path(cls, path: str | Path) -> "Supervisor":
        return cls(load_config(path))

    # ------------------------------------------------------------------ meta

    def _write_meta(self) -> None:
        atomic_write_json(
            self.meta_path,
            {
                "pid": os.getpid(),
                "version": "0.1.0",
                "started_at": time.time(),
                "started_at_iso": now_iso(),
                "state_dir": str(self.state_dir),
                "services": sorted(self.services),
            },
        )

    # ------------------------------------------------------------------ ctrl

    def recover(self) -> dict[str, str]:
        """Reconcile on-disk state with live processes. Call before start_all."""
        return {name: svc.recover() for name, svc in self.services.items()}

    def start_all(self) -> None:
        self._write_meta()
        for svc in self.services.values():
            svc.start()

    def stop_all(self) -> None:
        # Stop in parallel so graceful timeouts overlap.
        threads = [
            threading.Thread(target=svc.stop, name=f"stop-{name}", daemon=True)
            for name, svc in self.services.items()
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for svc in self.services.values():
            svc.close()

    def status(self) -> dict:
        return {name: svc.snapshot() for name, svc in self.services.items()}

    # ------------------------------------------------------------------- run

    def run(self) -> None:
        """Foreground loop: recover, start, wait for SIGINT/SIGTERM, stop."""
        self.recover()
        self.start_all()

        def _handler(signum, frame):
            self._shutdown.set()

        old = {}
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                old[sig] = signal.signal(sig, _handler)
        try:
            while not self._shutdown.wait(0.5):
                self._write_meta()
        finally:
            self.stop_all()
            for sig, handler in old.items():
                signal.signal(sig, handler)


def read_status(state_dir: str | Path) -> dict:
    """Read persisted status without a running supervisor (for the CLI)."""
    state_dir = Path(state_dir)
    out = {"supervisor": read_json(state_dir / "supervisor.json"), "services": {}}
    for path in sorted(state_dir.glob("*.state.json")):
        data = read_json(path)
        if data:
            out["services"][data.get("name", path.stem)] = data
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="supervisor")
    parser.add_argument("action", choices=("run", "status", "stop"))
    parser.add_argument("config", help="path to the TOML config file")
    args = parser.parse_args(argv)

    if args.action == "run":
        Supervisor.from_path(args.config).run()
        return 0

    config = load_config(args.config)
    if args.action == "status":
        print(json.dumps(read_status(config.state_dir), indent=2, sort_keys=True))
        return 0

    # stop: signal the running supervisor recorded in supervisor.json
    meta = read_json(config.state_dir / "supervisor.json")
    if not meta or not meta.get("pid"):
        print("no supervisor metadata found; nothing to stop")
        return 1
    os.kill(meta["pid"], signal.SIGTERM)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
