#!/usr/bin/env bash
# Export source for all three sites. Never copy workstation DBs, tokens, keys, or venvs.
set -euo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
destination="${1:?Usage: scripts/export-source.sh /absolute/path/anchor-source.tar.gz}"
case "$destination" in /*) ;; *) echo 'Use an absolute destination path.' >&2; exit 1 ;; esac
if [ -e "$destination" ]; then
    echo "Refusing to overwrite existing archive: $destination" >&2
    exit 1
fi
COPYFILE_DISABLE=1 tar --exclude='__pycache__' --exclude='*.pyc' --exclude='*.egg-info' \
    --exclude='.DS_Store' --exclude='._*' --exclude='anchor_config.h' --exclude='ota.env' \
    --exclude='queue.db*' --exclude='*.local.json' --exclude='*.local.ini' \
    --exclude='data' --exclude='logs' --exclude='backups' --exclude='*.db*' --exclude='*.sqlite*' \
    --exclude='.env' --exclude='*.key' --exclude='*.pem' --exclude='*.crt' \
    -czf "$destination" -C "$repo_root" \
    README.md pyproject.toml constraints-tested.txt .gitignore .dockerignore src tests scripts deploy hardware
printf 'Source archive: %s\n' "$destination"
