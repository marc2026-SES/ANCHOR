#!/usr/bin/env bash
# 선박 Pi에서 실행: 분석 AE를 정확히 1개로 재시작한다.
#   ssh <pi> 'bash ~/anchor-ai/tools/restart_ai.sh'
#
# pkill -f 는 명령줄 전체를 보므로, ssh 한 줄에 pkill과 기동 명령을 함께 쓰면 그 ssh 셸
# 자신(명령줄에 analytics_ae.py 포함)까지 죽는다. 재시작은 반드시 이 스크립트를 거친다.
set -u
cd "$(dirname "$0")/.."

pkill -f "analytics_ae.py --config" 2>/dev/null
sleep 2
pkill -9 -f "analytics_ae.py --config" 2>/dev/null

setsid .venv/bin/python -u analytics_ae.py --config config.pi.yaml >> ai.log 2>&1 < /dev/null &
sleep 3
echo "AE 인스턴스: $(pgrep -cf 'python -u analytics_ae.py')"
