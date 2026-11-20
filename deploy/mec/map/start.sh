#!/bin/sh
pgrep -f "^python3 .*map_server.py" >/dev/null && exit 0
cd "$(dirname "$0")" && setsid nohup python3 "$PWD/map_server.py" >> "$PWD/map.log" 2>&1 < /dev/null &
