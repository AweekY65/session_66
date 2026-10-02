"""Local image loading. Only PNG/JPEG files on the local filesystem."""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
from PIL import Image

SUPPORTED_SUFFIXES = {".png", ".jpg", ".jpeg"}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_image(path: Path) -> np.ndarray:
    """Load a local PNG/JPEG as an HxWx3 uint8 RGB array."""
    path = Path(path)
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError(f"unsupported image type: {path.suffix}")
    with Image.open(path) as im:
        im = im.convert("RGB")
        return np.asarray(im, dtype=np.uint8).copy()


def encode_png(arr: np.ndarray) -> bytes:
    """Encode an RGB array as PNG bytes (deterministic, no timestamps)."""
    import io

    im = Image.fromarray(arr, mode="RGB")
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()
