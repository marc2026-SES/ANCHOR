#!/usr/bin/env bash
# Export source for BOTH Pis. Never copy workstation DBs, tokens, keys, or venvs.
set -euo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
destination="${1:?Usage: scripts/export-source.sh /absolute/path/anchor-source.tar.gz}"
case "$destination" in /*) ;; *) echo 'Use an absolute destination path.' >&2; exit 1 ;; esac
if [ -e "$destination" ]; then
    echo "Refusing to overwrite existing archive: $destination" >&2
    exit 1
fi
COPYFILE_DISABLE=1 tar --exclude='__pycache__' --exclude='*.pyc' --exclude='*.egg-info' \
    --exclude='.DS_Store' --exclude='._*' \
    -czf "$destination" -C "$repo_root" \
    README.md pyproject.toml constraints-tested.txt .gitignore src tests scripts
printf 'Source archive: %s\n' "$destination"
