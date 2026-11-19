#!/usr/bin/env bash
# 선박 Pi에 anchor-ai를 설치하고 상주시키는 배포 스크립트 (맥에서 실행).
#
# 전제: Pi에 SSH 키 등록 완료 (ssh-copy-id -i ~/.ssh/anchor_pi.pub pi@<주소>)
# 사용: tools/deploy_pi.sh [user@host]      # 기본 pi@100.114.83.111 (Tailscale)
#
# 하는 일: 코드+학습CSV 복사 → venv·패키지 → Pi 로컬 설정 생성(CSE=localhost)
#          → Pi에서 재학습(Python 3.13 호환) → nohup 기동 + @reboot 자동 시작
set -euo pipefail
cd "$(dirname "$0")/.."

HOST=${1:-pi@100.114.83.111}
KEY=${KEY:-$HOME/.ssh/anchor_pi}
S() { ssh -i "$KEY" -o ConnectTimeout=10 "$HOST" "$@"; }

echo "== 1/5 코드 복사 ($HOST) =="
rsync -az -e "ssh -i $KEY" \
  --exclude .git --exclude .venv --exclude __pycache__ \
  --exclude data --exclude results --exclude models --exclude models-ship \
  ./ "$HOST:~/anchor-ai/"
S "mkdir -p ~/anchor-ai/data/ship"
rsync -az -e "ssh -i $KEY" data/ship/train.csv "$HOST:~/anchor-ai/data/ship/train.csv"

echo "== 2/5 venv·패키지 =="
S 'cd ~/anchor-ai && python3 -m venv .venv 2>/dev/null || true
   .venv/bin/pip -q install -U pip
   .venv/bin/pip -q install numpy pandas scikit-learn joblib PyYAML requests'

echo "== 3/5 Pi 로컬 설정 생성 (CSE=localhost, 미러·레지스트리는 Tailscale 유지) =="
S 'cd ~/anchor-ai && .venv/bin/python - <<PY
import yaml
cfg = yaml.safe_load(open("config.live.yaml"))
cfg["onem2m"]["cse_url"] = "http://127.0.0.1:8080"   # 같은 Pi 위의 ship CSE
yaml.safe_dump(cfg, open("config.pi.yaml", "w"), allow_unicode=True, sort_keys=False)
print("config.pi.yaml 생성")
PY'

echo "== 4/5 Pi에서 재학습 (Python 3.13 호환 모델) =="
S 'cd ~/anchor-ai && .venv/bin/python model_iforest.py train --profile ship \
     --csv data/ship/train.csv --models-dir models-ship'

echo "== 5/5 기동 + 재부팅 자동 시작 =="
S 'cd ~/anchor-ai && pkill -f "[a]nalytics_ae.py --config config.pi.yaml" 2>/dev/null || true
   sleep 1
   nohup .venv/bin/python -u analytics_ae.py --config config.pi.yaml > ai.log 2>&1 &
   sleep 3 && tail -5 ai.log'
S '(crontab -l 2>/dev/null | grep -v "anchor-ai/analytics"; \
    echo "@reboot cd ~/anchor-ai && .venv/bin/python -u analytics_ae.py --config config.pi.yaml >> ai.log 2>&1") | crontab -'

echo
echo "배포 완료. 로그 보기:  ssh -i $KEY $HOST 'tail -f ~/anchor-ai/ai.log'"
echo "⚠️ 맥에서 돌던 AE는 중복 기록 방지를 위해 꺼야 함"
