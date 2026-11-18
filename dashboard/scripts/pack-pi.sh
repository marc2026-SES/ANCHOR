#!/bin/sh
# Build the dashboard and pack what the shore Pi needs into anchor-dashboard.tgz.
# No config, tokens or data go in: secrets stay out of the bundle.
set -e
cd "$(dirname "$0")/.."
npm run build
COPYFILE_DISABLE=1 tar czf anchor-dashboard.tgz --exclude='__pycache__' \
  server/shore_monitor.py server/deny_tests.py server/shore-monitor.pi.example.json \
  deploy/anchor-shore-monitor.service dist
echo "anchor-dashboard.tgz ready: scp anchor-dashboard.tgz pi@shore.local:~"
