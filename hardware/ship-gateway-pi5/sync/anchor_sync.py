#!/usr/bin/env python3
"""Compatibility launcher: the old SQLite/FIFO synchronizer is replaced by ANCHOR core.

Set ANCHOR_CONFIG to the ship deployment JSON. Existing queue.db is left untouched;
retained raw CSE history is re-discovered by the new runtime.
"""
import os
from anchor.cli import main

if __name__ == "__main__":
    config = os.environ.get("ANCHOR_CONFIG")
    if not config:
        raise SystemExit("Set ANCHOR_CONFIG=/path/to/anchor/deploy/ship/core.json; see root README migration steps")
    main(["--config", config, "serve"])
