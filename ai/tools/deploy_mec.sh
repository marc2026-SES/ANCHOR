#!/usr/bin/env bash
# 연안 MEC 서버에 fleet-ai(선단 스캐너)를 설치하고 상주시킨다 (맥에서 실행).
#   tools/deploy_mec.sh <계정>@100.96.155.4
#
# 전제: 그 계정에 SSH 키가 등록돼 있다 (ssh-copy-id -i ~/.ssh/anchor_pi.pub <계정>@100.96.155.4).
# 스캐너는 한 곳에서만 돌려야 한다 — 두 곳에서 돌면 같은 경고가 두 번 발행되고 경로 변경도
# 겹친다. 설치가 끝나면 선박 Pi의 스캐너를 멈추고 crontab에서 뺀다 (끝에 명령 안내).
set -euo pipefail
cd "$(dirname "$0")/.."

HOST=${1:?사용법: tools/deploy_mec.sh <계정>@<MEC 서버>}
KEY=${KEY:-$HOME/.ssh/anchor_pi}
S() { ssh -i "$KEY" -o ConnectTimeout=10 "$HOST" "$@"; }

echo "== 1/4 코드 복사 ($HOST) =="
S "mkdir -p ~/anchor-ai/tools ~/anchor-ai/certs ~/anchor-ai/results"
scp -i "$KEY" -q onem2m_client.py route_conflict.py link_forecast.py fleet_route_model.py "$HOST:~/anchor-ai/"
scp -i "$KEY" -q tools/fleet_scan.py tools/restart_fleet.sh "$HOST:~/anchor-ai/tools/"
scp -i "$KEY" -q certs/mec-sandbox.pem "$HOST:~/anchor-ai/certs/"

echo "== 2/4 venv·패키지·자체 테스트 =="
S 'cd ~/anchor-ai && python3 -m venv .venv && .venv/bin/pip -q install -U pip requests numpy scikit-learn joblib \
   && .venv/bin/python tools/fleet_scan.py --selftest | tail -2 \
   && .venv/bin/python fleet_route_model.py selftest'

echo "== 3/4 재부팅 자동 시작 등록 =="
LINE='@reboot cd ~/anchor-ai && setsid .venv/bin/python -u tools/fleet_scan.py --base-url https://100.96.155.4/sbx3btt489/mep1/location/v3 --ca certs/mec-sandbox.pem --time-scale 20 --interval 2 --minutes 10080 --cse-url http://100.96.155.4:30080 --map-url http://100.96.155.4:30793 --reroute --track-log results/tracks.jsonl --route-models models-fleet >> fleet.log 2>&1 < /dev/null'
S "(crontab -l 2>/dev/null | grep -v fleet_scan.py; echo '$LINE') | crontab -"

echo "== 4/4 기동 =="
S 'bash ~/anchor-ai/tools/restart_fleet.sh && sleep 8 && tail -3 ~/anchor-ai/fleet.log'

echo
echo "설치 완료. 선박 Pi의 스캐너를 멈추고 자동 시작에서 빼려면:"
echo "  ssh -i $KEY will0623@100.114.83.111 'pkill -f \"[f]leet_scan.py --base-url\"; crontab -l | grep -v fleet_scan.py | crontab -'"
