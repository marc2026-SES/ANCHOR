#!/usr/bin/env bash
# Wireless firmware update through the ship Pi (PC -> ssh -> ship Pi -> Wi-Fi -> ESP32).
#   usage: ./ota.sh <bridge|engine|cargo> <esp32-ip>
#   the node's IP is printed on its serial log ("[OTA] ready: ...") and shown in the AP client list
#
# Settings come from the environment or from firmware/ota.env (not committed), e.g.
#   ANCHOR_PI=pi@192.168.50.1          # ssh target of the ship Pi
#   ANCHOR_OTA_PASS=change-me          # must match OTA_PASS in anchor_config.h
#   ARDUINO_CLI="/path/to/arduino-cli" # default: arduino-cli on PATH
# The ship Pi needs espota.py in ~/anchor/ota/ (copy it from the esp32 Arduino core's tools folder).
set -euo pipefail
node=${1:?node: bridge|engine|cargo}
ip=${2:?esp32 ip}
here=$(cd "$(dirname "$0")" && pwd)
[ -f "$here/ota.env" ] && . "$here/ota.env"
PI=${ANCHOR_PI:?set ANCHOR_PI (ssh target of the ship Pi) or create firmware/ota.env}
PASS=${ANCHOR_OTA_PASS:-change-me}
CLI=${ARDUINO_CLI:-arduino-cli}
case $node in
  bridge) fqbn=esp32:esp32:esp32s3 ;;
  engine) fqbn=esp32:esp32:esp32 ;;
  cargo)  fqbn=esp32:esp32:esp32c6:CDCOnBoot=cdc ;;
  *) echo "unknown node $node"; exit 1 ;;
esac
out=$(mktemp -d)  # ASCII temp path: the linker fails on non-ASCII project paths
"$CLI" compile --fqbn "$fqbn" --library "$here/libraries/AnchorNode" --output-dir "$out" "$here/node_$node"
scp -q "$out/node_$node.ino.bin" "$PI:anchor/ota/node_$node.bin"
ssh "$PI" "cd ~/anchor/ota && python3 espota.py -i $ip -p 3232 -a '$PASS' -f node_$node.bin"
rm -rf "$out"
