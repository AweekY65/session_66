"""Stays alive until SIGTERM, then exits 0 gracefully."""

import signal
import sys
import time

def _term(signum, frame):
    print("stay_up: got SIGTERM, exiting", flush=True)
    sys.exit(0)

signal.signal(signal.SIGTERM, _term)
print("stay_up: ready", flush=True)
while True:
    time.sleep(0.1)
