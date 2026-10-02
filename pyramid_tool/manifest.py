"""Manifest model: JSON document describing source, levels and tiles."""
from __future__ import annotations

import json
from pathlib import Path

MANIFEST_VERSION = 1
MANIFEST_NAME = "manifest.json"


def tile_relpath(level: int, x: int, y: int) -> str:
    return f"tiles/{level}/{x}_{y}.png"


def build_manifest(source_info: dict, config_dict: dict, levels: list) -> dict:
    return {
        "version": MANIFEST_VERSION,
        "source": source_info,
        "config": config_dict,
        "levels": levels,
    }


def load_manifest(out_dir: Path) -> dict | None:
    path = Path(out_dir) / MANIFEST_NAME
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("version") != MANIFEST_VERSION:
        return None
    return data


def dump_manifest(manifest: dict) -> bytes:
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
