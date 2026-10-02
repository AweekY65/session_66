"""Incremental, crash-safe pyramid/tile builder.

All writes go through a temp file + os.replace so an interrupted run never
leaves a half-written file that the manifest could mistake for a valid tile.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .config import PyramidConfig
from .image_io import encode_png, load_image, sha256_bytes, sha256_file
from .manifest import (MANIFEST_NAME, build_manifest, dump_manifest,
                       load_manifest, tile_relpath)
from .tiler import build_pyramid, level_dimensions, slice_level, tile_grid


def atomic_write(path: Path, data: bytes) -> None:
    """Write bytes to path atomically (temp file in same dir + rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@dataclass
class BuildStats:
    total_tiles: int = 0
    written_tiles: int = 0
    skipped_tiles: int = 0
    regenerated_tiles: int = 0   # missing or corrupt tiles that were rebuilt
    levels: int = 0
    manifest_written: bool = False
    errors: list = field(default_factory=list)


def _tile_index(manifest: dict) -> dict:
    idx = {}
    for lvl in manifest.get("levels", []):
        for t in lvl.get("tiles", []):
            idx[(lvl["level"], t["x"], t["y"])] = t
    return idx


def _verify_tile(out_dir: Path, entry: dict) -> bool:
    path = Path(out_dir) / entry["file"]
    if not path.exists():
        return False
    return sha256_file(path) == entry.get("sha256")


def build(source_path: Path, out_dir: Path, config: PyramidConfig) -> BuildStats:
    source_path = Path(source_path)
    out_dir = Path(out_dir)
    stats = BuildStats()

    # Remove stale temp files from any previously interrupted run.
    if out_dir.exists():
        for tmp in out_dir.rglob("*.tmp-*"):
            try:
                tmp.unlink()
            except OSError:
                pass

    source_bytes = source_path.read_bytes()
    source_hash = sha256_bytes(source_bytes)

    old = load_manifest(out_dir)
    old_tiles = {}
    if old and old.get("source", {}).get("sha256") == source_hash \
            and old.get("config") == config.to_dict():
        old_tiles = _tile_index(old)

    source = load_image(source_path)
    h, w = source.shape[:2]
    dims = level_dimensions(w, h, config.min_size)
    stats.levels = len(dims)

    # Decide which tiles must be (re)generated before doing any resampling.
    needed = []  # (level, x, y)
    for level, (lw, lh) in enumerate(dims):
        tx, ty = tile_grid(lw, lh, config.tile_size)
        for y in range(ty):
            for x in range(tx):
                stats.total_tiles += 1
                entry = old_tiles.get((level, x, y))
                if entry is not None and _verify_tile(out_dir, entry):
                    stats.skipped_tiles += 1
                else:
                    needed.append((level, x, y))
                    if entry is not None:
                        stats.regenerated_tiles += 1

    if not needed:
        return stats  # fully up to date; touch nothing

    needed_by_level = {}
    for level, x, y in needed:
        needed_by_level.setdefault(level, set()).add((x, y))

    pyramid = build_pyramid(source, config)

    manifest_levels = []
    for level, img in enumerate(pyramid):
        lh, lw = img.shape[:2]
        tx, ty = tile_grid(lw, lh, config.tile_size)
        level_entry = {
            "level": level,
            "width": lw,
            "height": lh,
            "tiles_x": tx,
            "tiles_y": ty,
            "tiles": [],
        }
        for tile in slice_level(level, img, config):
            rel = tile_relpath(tile.level, tile.x, tile.y)
            old_entry = old_tiles.get((tile.level, tile.x, tile.y))
            if (tile.x, tile.y) not in needed_by_level.get(level, set()) \
                    and old_entry is not None:
                level_entry["tiles"].append(old_entry)
                continue
            data = encode_png(tile.pixels)
            atomic_write(out_dir / rel, data)
            stats.written_tiles += 1
            level_entry["tiles"].append({
                "x": tile.x,
                "y": tile.y,
                "width": tile.width,
                "height": tile.height,
                "content_width": tile.content_width,
                "content_height": tile.content_height,
                "file": rel,
                "sha256": sha256_bytes(data),
            })
        level_entry["tiles"].sort(key=lambda t: (t["y"], t["x"]))
        manifest_levels.append(level_entry)

    manifest = build_manifest(
        source_info={
            "path": str(source_path),
            "sha256": source_hash,
            "width": w,
            "height": h,
        },
        config_dict=config.to_dict(),
        levels=manifest_levels,
    )
    atomic_write(out_dir / MANIFEST_NAME, dump_manifest(manifest))
    stats.manifest_written = True
    return stats
