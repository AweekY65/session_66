"""CLI: python -m pyramid_tool build <image> -o <out_dir> [options]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .builder import build
from .config import PyramidConfig


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="pyramid_tool",
        description="Build a local image pyramid with tiles and a manifest.")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="build or incrementally rebuild a pyramid")
    b.add_argument("image", type=Path, help="local PNG/JPEG input image")
    b.add_argument("-o", "--out", type=Path, required=True, help="output directory")
    b.add_argument("--tile-size", type=int, default=256)
    b.add_argument("--min-size", type=int, default=256,
                   help="stop when max(width, height) <= min-size")
    b.add_argument("--resample", choices=["nearest", "bilinear"], default="bilinear")
    b.add_argument("--edge-mode", choices=["pad", "crop"], default="pad")
    args = parser.parse_args(argv)

    config = PyramidConfig(
        tile_size=args.tile_size,
        min_size=args.min_size,
        resample=args.resample,
        edge_mode=args.edge_mode,
    )
    stats = build(args.image, args.out, config)
    print(f"levels={stats.levels} total_tiles={stats.total_tiles} "
          f"written={stats.written_tiles} skipped={stats.skipped_tiles} "
          f"regenerated={stats.regenerated_tiles} "
          f"manifest_written={stats.manifest_written}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
