"""Configuration loading for the local supervisor.

Config is a local TOML file. Example:

    [supervisor]
    state_dir = ".sv"

    [[service]]
    name = "worker"
    command = ["python3", "worker.py"]
    restart = "on-failure"        # never | on-failure | always
    max_restarts = 5
    backoff_initial = 0.5         # seconds
    backoff_factor = 2.0
    backoff_max = 30.0            # seconds
    backoff_reset_after = 10.0    # stable-run seconds that reset backoff
    stop_signal = "TERM"
    stop_timeout = 5.0            # graceful seconds before SIGKILL
    log_max_bytes = 1048576
    log_backups = 3
"""

from __future__ import annotations

import dataclasses
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

RESTART_POLICIES = ("never", "on-failure", "always")


@dataclass
class ServiceConfig:
    name: str
    command: list[str]
    restart: str = "on-failure"
    max_restarts: int = 5
    backoff_initial: float = 0.5
    backoff_factor: float = 2.0
    backoff_max: float = 30.0
    backoff_reset_after: float = 10.0
    stop_signal: str = "TERM"
    stop_timeout: float = 5.0
    log_max_bytes: int = 10 * 1024 * 1024
    log_backups: int = 3
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)

    def validate(self) -> None:
        if not self.name:
            raise ValueError("service name must not be empty")
        if not self.command:
            raise ValueError(f"service {self.name!r}: command must not be empty")
        if self.restart not in RESTART_POLICIES:
            raise ValueError(
                f"service {self.name!r}: restart must be one of {RESTART_POLICIES}"
            )
        if self.max_restarts < 0:
            raise ValueError(f"service {self.name!r}: max_restarts must be >= 0")
        if self.backoff_initial < 0 or self.backoff_max < 0:
            raise ValueError(f"service {self.name!r}: backoff values must be >= 0")
        if self.backoff_factor < 1.0:
            raise ValueError(f"service {self.name!r}: backoff_factor must be >= 1.0")
        if self.stop_timeout < 0:
            raise ValueError(f"service {self.name!r}: stop_timeout must be >= 0")
        if self.log_max_bytes <= 0:
            raise ValueError(f"service {self.name!r}: log_max_bytes must be > 0")
        if self.log_backups < 0:
            raise ValueError(f"service {self.name!r}: log_backups must be >= 0")


@dataclass
class SupervisorConfig:
    state_dir: Path
    services: list[ServiceConfig]

    @property
    def log_dir(self) -> Path:
        return self.state_dir / "logs"


def _parse_command(raw: object) -> list[str]:
    if isinstance(raw, str):
        return shlex.split(raw)
    if isinstance(raw, list) and all(isinstance(x, str) for x in raw):
        return list(raw)
    raise ValueError(f"invalid command: {raw!r}")


def load_config(path: str | Path) -> SupervisorConfig:
    path = Path(path)
    with path.open("rb") as fh:
        data = tomllib.load(fh)

    base = path.resolve().parent
    sup = data.get("supervisor", {})
    state_dir = Path(sup.get("state_dir", ".sv"))
    if not state_dir.is_absolute():
        state_dir = base / state_dir

    services: list[ServiceConfig] = []
    names: set[str] = set()
    for raw in data.get("service", []):
        raw = dict(raw)
        raw["command"] = _parse_command(raw.get("command"))
        known = {f.name for f in dataclasses.fields(ServiceConfig)}
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"unknown service keys: {sorted(unknown)}")
        cfg = ServiceConfig(**raw)
        cfg.validate()
        if cfg.name in names:
            raise ValueError(f"duplicate service name: {cfg.name!r}")
        names.add(cfg.name)
        services.append(cfg)

    if not services:
        raise ValueError("config defines no services")
    return SupervisorConfig(state_dir=state_dir, services=services)
