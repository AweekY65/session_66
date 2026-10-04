"""Prints a line and exits 0 (optionally after a delay)."""

import sys
import time

delay = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0
print("normal: start", flush=True)
time.sleep(delay)
print("normal: done", flush=True)
sys.exit(0)
