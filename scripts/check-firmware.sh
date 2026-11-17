#!/usr/bin/env bash
# Compile all three physical board targets. Does not upload or operate hardware.
set -euo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
firmware_dir="$repo_root/hardware/firmware"
command -v arduino-cli >/dev/null || { echo 'Install arduino-cli, ESP32 3.3.x, ArduinoJson 7, PubSubClient and DHT sensor library.' >&2; exit 1; }
test -f "$firmware_dir/libraries/AnchorNode/anchor_config.h" || { echo 'Copy anchor_config.example.h to anchor_config.h first.' >&2; exit 1; }
build_dir="$(mktemp -d /tmp/anchor-firmware.XXXXXX)"
trap 'rm -rf -- "$build_dir"' EXIT
for board in bridge engine cargo; do
  test -f "$firmware_dir/libraries/AnchorNode/anchor_${board}_tls.h" || { echo "Missing TLS header for $board. Run scripts/provision-tls.py first." >&2; exit 1; }
  case "$board" in
    bridge) fqbn='esp32:esp32:esp32s3' ;;
    engine) fqbn='esp32:esp32:esp32' ;;
    cargo) fqbn='esp32:esp32:esp32c6:CDCOnBoot=cdc' ;;
  esac
  arduino-cli compile --fqbn "$fqbn" --library "$firmware_dir/libraries/AnchorNode" \
    --output-dir "$build_dir/$board" "$firmware_dir/node_$board"
done
