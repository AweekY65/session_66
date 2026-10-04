"""Crashes with exit code 1, optionally only N times (tracked via a counter
file), then stays alive. Usage: crash.py [counter_file] [max_crashes]"""

import os
import signal
import sys
import time

counter_file = sys.argv[1] if len(sys.argv) > 1 else None
max_crashes = int(sys.argv[2]) if len(sys.argv) > 2 else 1 << 30

count = 0
if counter_file and os.path.exists(counter_file):
    with open(counter_file) as fh:
        count = int(fh.read().strip() or "0")

if count < max_crashes:
    if counter_file:
        with open(counter_file, "w") as fh:
            fh.write(str(count + 1))
    print(f"crash: attempt {count + 1}, exiting 1", flush=True)
    sys.exit(1)

print("crash: stable now", flush=True)
signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
while True:
    time.sleep(0.1)
