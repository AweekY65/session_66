"""Automated tests for pyramid_tool.

All checks run in the terminal; no image windows are ever opened.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from pyramid_tool import PyramidConfig, build
from pyramid_tool.builder import atomic_write
from pyramid_tool.image_io import load_image, sha256_file
from pyramid_tool.manifest import MANIFEST_NAME, load_manifest
from pyramid_tool.resample import resize_bilinear, resize_nearest
from pyramid_tool.tiler import level_dimensions, slice_level, tile_grid


def make_image(path: Path, width: int, height: int, fmt: str = "PNG") -> np.ndarray:
    """Create a deterministic gradient test image on disk."""
    rng = np.arange(width * height * 3, dtype=np.uint32).reshape(height, width, 3)
    arr = ((rng * 37 + 11) % 256).astype(np.uint8)
    Image.fromarray(arr, mode="RGB").save(path, format=fmt)
    return arr


def read_tile(out_dir: Path, rel: str) -> np.ndarray:
    with Image.open(out_dir / rel) as im:
        return np.asarray(im.convert("RGB"), dtype=np.uint8)


# ---------------------------------------------------------------- level math

def test_level_dimensions_odd_sizes():
    # 53x37 with min_size=8 -> 53x37, 27x19, 14x10, 7x5 (stop: max<=8)
    assert level_dimensions(53, 37, 8) == [(53, 37), (27, 19), (14, 10), (7, 5)]


def test_level_dimensions_stops_at_min_size():
    assert level_dimensions(256, 256, 256) == [(256, 256)]
    assert level_dimensions(1, 1, 1) == [(1, 1)]


def test_tile_grid_rounds_up():
    assert tile_grid(10, 10, 4) == (3, 3)
    assert tile_grid(8, 8, 4) == (2, 2)


# ------------------------------------------------------------------ resample

def test_nearest_known_values():
    src = np.arange(16, dtype=np.uint8).reshape(4, 4, 1).repeat(3, axis=2) * 10
    out = resize_nearest(src, 2, 2)
    # align-centers: picks source rows/cols 1 and 3
    expected = src[np.ix_([1, 3], [1, 3])]
    np.testing.assert_array_equal(out, expected)


def test_bilinear_known_values():
    src = np.zeros((2, 2, 3), dtype=np.uint8)
    src[0, 0] = (10, 20, 30)
    src[0, 1] = (30, 40, 50)
    src[1, 0] = (50, 60, 70)
    src[1, 1] = (70, 80, 90)
    out = resize_bilinear(src, 1, 1)
    assert out.shape == (1, 1, 3)
    np.testing.assert_array_equal(out[0, 0], [40, 50, 60])  # exact average


def test_resample_deterministic():
    rng = np.random.default_rng(42)
    src = rng.integers(0, 256, size=(37, 53, 3), dtype=np.uint8)
    for _ in range(3):
        a = resize_bilinear(src, 19, 27)
        b = resize_bilinear(src, 19, 27)
        np.testing.assert_array_equal(a, b)
        c = resize_nearest(src, 19, 27)
        d = resize_nearest(src, 19, 27)
        np.testing.assert_array_equal(c, d)


# ------------------------------------------------------------------ slicing

def config(**kw):
    defaults = dict(tile_size=4, min_size=4, resample="bilinear", edge_mode="pad")
    defaults.update(kw)
    return PyramidConfig(**defaults)


def test_edge_tiles_pad_mode():
    arr = np.full((10, 10, 3), 200, dtype=np.uint8)
    tiles = list(slice_level(0, arr, config(edge_mode="pad", pad_color=(1, 2, 3))))
    assert len(tiles) == 9
    edge = [t for t in tiles if (t.x, t.y) == (2, 2)][0]
    assert (edge.width, edge.height) == (4, 4)          # padded to full tile
    assert (edge.content_width, edge.content_height) == (2, 2)
    assert tuple(edge.pixels[0, 0]) == (200, 200, 200)  # image content
    assert tuple(edge.pixels[3, 3]) == (1, 2, 3)        # padding fill


def test_edge_tiles_crop_mode():
    arr = np.full((10, 10, 3), 200, dtype=np.uint8)
    tiles = list(slice_level(0, arr, config(edge_mode="crop")))
    edge = [t for t in tiles if (t.x, t.y) == (2, 2)][0]
    assert (edge.width, edge.height) == (2, 2)          # stored at real size
    assert edge.pixels.shape == (2, 2, 3)
    inner = [t for t in tiles if (t.x, t.y) == (0, 0)][0]
    assert (inner.width, inner.height) == (4, 4)


# -------------------------------------------------------------------- build

def test_full_build_and_manifest(tmp_path):
    src = tmp_path / "src.png"
    make_image(src, 53, 37)
    out = tmp_path / "out"
    stats = build(src, out, config())
    # 53x37 -> 27x19 -> 14x10 -> 7x5 -> 4x3 (min_size=4)
    assert stats.levels == 5
    assert stats.written_tiles == stats.total_tiles
    assert stats.manifest_written

    m = load_manifest(out)
    assert m["source"]["width"] == 53 and m["source"]["height"] == 37
    assert [l["level"] for l in m["levels"]] == [0, 1, 2, 3, 4]
    assert m["levels"][1]["width"] == 27 and m["levels"][1]["height"] == 19
    # every tile hash in the manifest matches the file on disk
    for lvl in m["levels"]:
        for t in lvl["tiles"]:
            assert sha256_file(out / t["file"]) == t["sha256"]


def test_build_jpeg_input(tmp_path):
    src = tmp_path / "src.jpg"
    make_image(src, 20, 15, fmt="JPEG")
    out = tmp_path / "out"
    stats = build(src, out, config())
    assert stats.manifest_written
    assert load_manifest(out)["source"]["width"] == 20


def test_build_deterministic(tmp_path):
    src = tmp_path / "src.png"
    make_image(src, 53, 37)
    for mode in ("nearest", "bilinear"):
        out_a, out_b = tmp_path / "a", tmp_path / "b"
        cfg = config(resample=mode)
        build(src, out_a, cfg)
        build(src, out_b, cfg)
        ma = (out_a / MANIFEST_NAME).read_bytes()
        mb = (out_b / MANIFEST_NAME).read_bytes()
        assert ma == mb, f"{mode} output not deterministic"


def test_incremental_build_no_changes(tmp_path):
    src = tmp_path / "src.png"
    make_image(src, 40, 30)
    out = tmp_path / "out"
    build(src, out, config())
    manifest_mtime = (out / MANIFEST_NAME).stat().st_mtime_ns

    stats = build(src, out, config())
    assert stats.written_tiles == 0
    assert stats.skipped_tiles == stats.total_tiles
    assert not stats.manifest_written
    assert (out / MANIFEST_NAME).stat().st_mtime_ns == manifest_mtime


def test_corrupt_tile_regenerated(tmp_path):
    src = tmp_path / "src.png"
    make_image(src, 20, 20)
    out = tmp_path / "out"
    build(src, out, config())
    m = load_manifest(out)
    victim = m["levels"][0]["tiles"][3]
    good_hash = victim["sha256"]
    (out / victim["file"]).write_bytes(b"corrupted!")

    stats = build(src, out, config())
    assert stats.regenerated_tiles == 1
    assert stats.written_tiles == 1
    assert sha256_file(out / victim["file"]) == good_hash


def test_missing_tile_regenerated(tmp_path):
    src = tmp_path / "src.png"
    make_image(src, 20, 20)
    out = tmp_path / "out"
    build(src, out, config())
    m = load_manifest(out)
    victim = m["levels"][1]["tiles"][0]
    os.unlink(out / victim["file"])

    stats = build(src, out, config())
    assert stats.written_tiles == 1
    assert (out / victim["file"]).exists()
    assert sha256_file(out / victim["file"]) == victim["sha256"]


def test_interrupted_build_leaves_no_half_files(tmp_path, monkeypatch):
    src = tmp_path / "src.png"
    make_image(src, 30, 30)
    out = tmp_path / "out"

    import pyramid_tool.builder as builder_mod

    real_write = builder_mod.atomic_write
    calls = {"n": 0}

    def flaky_write(path, data):
        calls["n"] += 1
        if calls["n"] == 3:
            # crash mid-write: leave a partial temp file behind
            tmp = Path(path).with_name(f".{Path(path).name}.tmp-{os.getpid()}")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(data[:5])
            raise RuntimeError("simulated crash")
        return real_write(path, data)

    monkeypatch.setattr(builder_mod, "atomic_write", flaky_write)
    with pytest.raises(RuntimeError):
        build(src, out, config())

    # manifest must not claim the half-finished build is valid
    assert not (out / MANIFEST_NAME).exists()

    # a fresh build recovers cleanly and sweeps stale temp files
    monkeypatch.setattr(builder_mod, "atomic_write", real_write)
    stats = build(src, out, config())
    assert stats.manifest_written
    assert list(out.rglob("*.tmp-*")) == []
    m = load_manifest(out)
    for lvl in m["levels"]:
        for t in lvl["tiles"]:
            assert sha256_file(out / t["file"]) == t["sha256"]


def test_config_change_triggers_full_rebuild(tmp_path):
    src = tmp_path / "src.png"
    make_image(src, 20, 20)
    out = tmp_path / "out"
    build(src, out, config(resample="nearest"))
    stats = build(src, out, config(resample="bilinear"))
    assert stats.written_tiles == stats.total_tiles


def test_atomic_write_replaces_and_cleans_tmp(tmp_path):
    p = tmp_path / "f.bin"
    atomic_write(p, b"one")
    atomic_write(p, b"two")
    assert p.read_bytes() == b"two"
    assert list(tmp_path.glob("*.tmp-*")) == []
