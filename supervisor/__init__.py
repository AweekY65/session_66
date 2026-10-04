"""Local process supervisor.

Manages multiple child processes defined by a local config file.
All state, logs and metadata live on the local filesystem or in memory;
no systemd / Docker / Kubernetes / database / external service is used.
"""

from .config import ServiceConfig, SupervisorConfig, load_config
from .service import Service, ServiceStatus
from .supervisor import Supervisor

__all__ = [
    "ServiceConfig",
    "SupervisorConfig",
    "load_config",
    "Service",
    "ServiceStatus",
    "Supervisor",
]

__version__ = "0.1.0"
