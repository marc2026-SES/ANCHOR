#!/usr/bin/env python3
"""ExecStartPre for the ship CSE (ACME, TinyDB).

TinyDB rewrites the whole JSON file on each write, so a power cut mid-write leaves a
truncated file and ACME then exits at startup. If any DB file fails to parse, move the
data directory aside so ACME starts with a fresh DB. The ESP32 nodes re-register their
AE/CNTs automatically.
"""
import json
import pathlib
import shutil
import sys
import time

data = pathlib.Path(__file__).resolve().parent / "data"
bad = []
for f in data.glob("*.json"):
    if f.stat().st_size == 0:
        continue
    try:
        json.loads(f.read_text())
    except ValueError as e:
        bad.append(f"{f.name}: {e}")

if bad:
    dst = data.parent.parent / ("ship-cse-db-corrupt-" + time.strftime("%Y%m%d-%H%M%S"))
    shutil.move(str(data), str(dst))
    print("corrupt DB moved to", dst, "|", "; ".join(bad), file=sys.stderr)
else:
    print("DB ok")
