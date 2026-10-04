"""Ignores SIGTERM forever; only SIGKILL can stop it."""

import signal
import sys
import time

def _term(signum, frame):
    print("ignore_term: ignoring SIGTERM", flush=True)

signal.signal(signal.SIGTERM, _term)
print("ignore_term: ready", flush=True)
while True:
    time.sleep(0.1)
