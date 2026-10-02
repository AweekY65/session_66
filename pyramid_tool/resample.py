"""Deterministic nearest / bilinear downscaling implemented with numpy.

Both filters use the "align centers" convention:
    src_coord = (dst_coord + 0.5) * (src_size / dst_size) - 0.5
The implementation only depends on numpy float64 arithmetic, so the same
input always produces bit-identical output regardless of Pillow version.
"""
from __future__ import annotations

import numpy as np


def _resample_axis_nearest(src: np.ndarray, new_len: int, axis: int) -> np.ndarray:
    old_len = src.shape[axis]
    scale = old_len / new_len
    idx = np.floor((np.arange(new_len) + 0.5) * scale).astype(np.int64)
    idx = np.clip(idx, 0, old_len - 1)
    return np.take(src, idx, axis=axis)


def resize_nearest(img: np.ndarray, new_h: int, new_w: int) -> np.ndarray:
    out = _resample_axis_nearest(img, new_h, axis=0)
    out = _resample_axis_nearest(out, new_w, axis=1)
    return out


def _bilinear_weights(old_len: int, new_len: int):
    scale = old_len / new_len
    coords = (np.arange(new_len, dtype=np.float64) + 0.5) * scale - 0.5
    coords = np.clip(coords, 0.0, old_len - 1.0)
    lo = np.floor(coords).astype(np.int64)
    hi = np.minimum(lo + 1, old_len - 1)
    frac = (coords - lo).astype(np.float64)
    return lo, hi, frac


def resize_bilinear(img: np.ndarray, new_h: int, new_w: int) -> np.ndarray:
    work = img.astype(np.float64)

    lo, hi, frac = _bilinear_weights(img.shape[0], new_h)
    top = work[lo]
    bot = work[hi]
    w = frac[:, None, None] if img.ndim == 3 else frac[:, None]
    work = top * (1.0 - w) + bot * w

    lo, hi, frac = _bilinear_weights(img.shape[1], new_w)
    left = work[:, lo]
    right = work[:, hi]
    w = frac[None, :, None] if img.ndim == 3 else frac[None, :]
    work = left * (1.0 - w) + right * w

    return np.clip(np.floor(work + 0.5), 0, 255).astype(np.uint8)


def resize(img: np.ndarray, new_h: int, new_w: int, mode: str) -> np.ndarray:
    if img.shape[0] == new_h and img.shape[1] == new_w:
        return img.copy()
    if mode == "nearest":
        return resize_nearest(img, new_h, new_w)
    if mode == "bilinear":
        return resize_bilinear(img, new_h, new_w)
    raise ValueError(f"unknown resample mode: {mode}")
