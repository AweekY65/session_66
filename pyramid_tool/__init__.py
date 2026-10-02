"""Local image pyramid and tile generator.

Everything (inputs, levels, tiles, manifest, caches) lives on the local
filesystem or in memory. No map server, CDN, cloud storage or any other
external service is used.
"""
from .builder import BuildStats, build
from .config import PyramidConfig

__all__ = ["build", "BuildStats", "PyramidConfig"]
__version__ = "0.1.0"
