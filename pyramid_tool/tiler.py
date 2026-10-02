"""Pyramid level computation and tile slicing."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, List, Tuple

import numpy as np

from .config import PyramidConfig
from .resample import resize


def level_dimensions(width: int, height: int, min_size: int) -> List[Tuple[int, int]]:
    """Dimensions of every pyramid level, starting at level 0 (the source).

    Each level halves both dimensions with ceil division (min 1 px).
    Generation stops once max(width, height) <= min_size.
    """
    dims = [(width, height)]
    while max(width, height) > min_size:
        width = max(1, (width + 1) // 2)
        height = max(1, (height + 1) // 2)
        dims.append((width, height))
    return dims


def tile_grid(width: int, height: int, tile_size: int) -> Tuple[int, int]:
    """Number of tiles along x and y for one level."""
    tx = (width + tile_size - 1) // tile_size
    ty = (height + tile_size - 1) // tile_size
    return tx, ty


@dataclass(frozen=True)
class Tile:
    level: int
    x: int           # tile column (0-based, left to right)
    y: int           # tile row (0-based, top to bottom)
    width: int       # stored pixel width of the tile image
    height: int      # stored pixel height of the tile image
    content_width: int   # pixels inside the tile that belong to the image
    content_height: int
    pixels: np.ndarray


def slice_level(level: int, img: np.ndarray, config: PyramidConfig) -> Iterator[Tile]:
    """Slice one level image into tiles.

    Tile (x, y) covers image pixels [x*ts, (x+1)*ts) x [y*ts, (y+1)*ts).
    Edge handling follows config.edge_mode ("pad" or "crop").
    """
    h, w = img.shape[:2]
    ts = config.tile_size
    tx, ty = tile_grid(w, h, ts)
    for y in range(ty):
        for x in range(tx):
            x0, y0 = x * ts, y * ts
            cw = min(ts, w - x0)
            ch = min(ts, h - y0)
            region = img[y0:y0 + ch, x0:x0 + cw]
            if config.edge_mode == "pad":
                if cw == ts and ch == ts:
                    tile_img = region.copy()
                else:
                    tile_img = np.empty((ts, ts, 3), dtype=np.uint8)
                    tile_img[:, :] = np.asarray(config.pad_color, dtype=np.uint8)
                    tile_img[:ch, :cw] = region
                yield Tile(level, x, y, ts, ts, cw, ch, tile_img)
            else:  # crop
                yield Tile(level, x, y, cw, ch, cw, ch, region.copy())


def build_pyramid(source: np.ndarray, config: PyramidConfig) -> List[np.ndarray]:
    """All level images, level 0 first."""
    h, w = source.shape[:2]
    levels = [source]
    for lw, lh in level_dimensions(w, h, config.min_size)[1:]:
        levels.append(resize(levels[-1], lh, lw, config.resample))
    return levels
