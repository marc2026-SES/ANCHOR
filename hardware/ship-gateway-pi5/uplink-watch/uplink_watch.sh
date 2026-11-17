#!/bin/sh
# ANCHOR shore-link watchdog (ship Pi 5).
# NetworkManager backs off its scans (up to ~2 min) and gives up after a few failed attempts once the
# uplink drops out of range, so the boat can come back into range and stay disconnected. While the
# uplink interface is not connected, rescan and ask NM to bring up the best known network every few s.
IF="${ANCHOR_UPLINK_IF:-wlan1}"
PERIOD="${ANCHOR_UPLINK_PERIOD:-6}"
was=""
while true; do
  state=$(nmcli -t -f DEVICE,STATE dev | awk -F: -v i="$IF" '$1==i{print $2}')
  if [ "$state" != "connected" ]; then
    [ "$was" = "connected" ] && echo "$IF lost (state=$state), retrying every ${PERIOD}s"
    nmcli dev wifi rescan ifname "$IF" >/dev/null 2>&1
    sleep 2
    if nmcli --wait 8 dev connect "$IF" >/dev/null 2>&1; then
      echo "$IF reconnected: $(nmcli -t -f GENERAL.CONNECTION dev show "$IF" | cut -d: -f2)"
      state=connected
    fi
  fi
  was="$state"
  sleep "$PERIOD"
done
