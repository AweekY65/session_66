"""Configuration for the local image pyramid / tile builder."""
from __future__ import annotations

from dataclasses import dataclass, asdict


RESAMPLE_MODES = ("nearest", "bilinear")
EDGE_MODES = ("pad", "crop")


@dataclass(frozen=True)
class PyramidConfig:
    """Build configuration.

    tile_size:  edge length (px) of one square tile.
    min_size:   stop generating levels once max(width, height) <= min_size.
    resample:   "nearest" or "bilinear".
    edge_mode:  "pad"  -> every tile is exactly tile_size x tile_size, the
                          area outside the image is filled with pad_color.
                "crop" -> edge tiles are stored at their real (smaller) size.
    pad_color:  RGB fill used by edge_mode="pad".
    """

    tile_size: int = 256
    min_size: int = 256
    resample: str = "bilinear"
    edge_mode: str = "pad"
    pad_color: tuple = (0, 0, 0)

    def __post_init__(self):
        if self.tile_size <= 0:
            raise ValueError("tile_size must be positive")
        if self.min_size <= 0:
            raise ValueError("min_size must be positive")
        if self.resample not in RESAMPLE_MODES:
            raise ValueError(f"resample must be one of {RESAMPLE_MODES}")
        if self.edge_mode not in EDGE_MODES:
            raise ValueError(f"edge_mode must be one of {EDGE_MODES}")
        if len(self.pad_color) != 3:
            raise ValueError("pad_color must be an RGB triple")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["pad_color"] = list(self.pad_color)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "PyramidConfig":
        d = dict(d)
        d["pad_color"] = tuple(d["pad_color"])
        return cls(**d)
