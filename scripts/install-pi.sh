#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
venv_dir="${ANCHOR_VENV:-$repo_root/.venv}"
acme_dir="${ANCHOR_ACME:-$(dirname -- "$repo_root")/ACME-oneM2M-CSE}"
acme_pin="283bd7d696090e983c07c149511102caab99642e"

if [ "$(id -u)" = 0 ]; then
    echo 'Run as the normal Pi user, not root.' >&2
    exit 1
fi
for dependency in python3 git; do
    command -v "$dependency" >/dev/null || { echo "Missing dependency: $dependency" >&2; exit 1; }
done
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
if [ ! -d "$acme_dir" ]; then
    git clone --no-checkout https://github.com/ankraft/ACME-oneM2M-CSE.git "$acme_dir"
    git -C "$acme_dir" checkout --detach "$acme_pin"
fi
if [ "$(git -C "$acme_dir" rev-parse HEAD)" != "$acme_pin" ]; then
    echo "Existing ACME checkout differs from the tested revision. Left unchanged: $acme_dir" >&2
    exit 1
fi
if [ -n "$(git -C "$acme_dir" status --porcelain)" ]; then
    echo "Existing ACME checkout is modified. Left unchanged: $acme_dir" >&2
    exit 1
fi
python3 -m venv "$venv_dir"
"$venv_dir/bin/python" -m pip install -e "$acme_dir" -e "$repo_root[test]"
printf '\nInstalled. Activate with: source "%s/bin/activate"\n' "$venv_dir"
echo 'Configure deploy/<ship|edge|shore>.json and the matching CSE. See README migration steps.'
