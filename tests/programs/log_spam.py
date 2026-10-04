"""Writes many numbered lines to stdout and stderr.
Usage: log_spam.py [count] [payload_len]"""

import sys

count = int(sys.argv[1]) if len(sys.argv) > 1 else 500
payload_len = int(sys.argv[2]) if len(sys.argv) > 2 else 40
payload = "x" * payload_len

for i in range(count):
    print(f"spam-{i:05d} {payload}", flush=True)
    print(f"errspam-{i:05d} {payload}", file=sys.stderr, flush=True)
