#!/usr/bin/env bash
# 선박 Pi에서 실행: fleet 스캐너를 crontab @reboot 줄과 같은 명령으로 재시작한다.
#   ssh <pi> 'bash ~/anchor-ai/tools/restart_fleet.sh'
set -u
line=$(crontab -l 2>/dev/null | grep "fleet_scan.py" | head -1 | sed 's/^@reboot //')
[ -n "$line" ] || { echo "crontab에 fleet_scan 항목이 없음"; exit 1; }

pkill -f "fleet_scan.py --base-url" 2>/dev/null
sleep 2
sh -c "$line" > /dev/null 2>&1 < /dev/null &
sleep 4
echo "fleet 스캐너: $(pgrep -cf 'python -u tools/fleet_scan.py')"
