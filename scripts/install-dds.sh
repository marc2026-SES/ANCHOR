#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
venv_dir="${ANCHOR_VENV:-$repo_root/.venv}"
dds_prefix="${ANCHOR_DDS_PREFIX:-$repo_root/.runtime-dds}"
dds_source="$repo_root/.runtime-dds-source"
dds_pin="179ae6fe82c9954c73aa5e31bd28f994a853dcf1"

# PyPI 11.1.0 has no Linux ARM64 wheel. Pi/MEC ARM hosts build the matching C library.
build_native=false
if [[ "$(uname -s)" == Linux && "$(uname -m)" != x86_64 ]]; then build_native=true; fi
if [[ "${1:-}" == --from-source ]]; then build_native=true; fi
if ! "$venv_dir/bin/python" -c 'import sys; assert (3, 10) <= sys.version_info[:2] < (3, 14)'; then build_native=true; fi
if "$build_native"; then
    for tool in git cmake cc; do
        command -v "$tool" >/dev/null || { echo "Install build prerequisites: git cmake build-essential" >&2; exit 1; }
    done
    if [[ ! -d "$dds_source" ]]; then
        git clone --branch 11.1.0 --depth 1 https://github.com/eclipse-cyclonedds/cyclonedds.git "$dds_source"
    fi
    [[ "$(git -C "$dds_source" rev-parse HEAD)" == "$dds_pin" ]] || { echo 'DDS source revision differs; left unchanged.' >&2; exit 1; }
    [[ -z "$(git -C "$dds_source" status --porcelain)" ]] || { echo 'DDS source has local modifications; left unchanged.' >&2; exit 1; }
    cmake -S "$dds_source" -B "$repo_root/.runtime-dds-build" -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX="$dds_prefix" -DBUILD_TESTING=OFF -DBUILD_EXAMPLES=OFF -DENABLE_SECURITY=OFF
    cmake --build "$repo_root/.runtime-dds-build" --parallel 2
    cmake --install "$repo_root/.runtime-dds-build"
    export CYCLONEDDS_HOME="$dds_prefix"
    "$venv_dir/bin/python" -m pip install --no-binary=cyclonedds 'cyclonedds==11.1.0'
fi
"$venv_dir/bin/python" -m pip install -e "$repo_root[dds]"
echo 'Native DDS installed. Configure DDS interface/peers and run deploy/<ship|edge>/run.sh dds-check.'
echo 'This profile uses UDP on a trusted/isolated link. It does not enable DDS Security.'
