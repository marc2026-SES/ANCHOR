#!/usr/bin/env bash
# 선박 Pi에서 실행: 정지 상태 N분을 수집해 기준선 모델을 다시 학습한다.
#   bash tools/rebaseline.sh [분=15]
#
# 수집하는 동안 배를 건드리면 그 움직임이 '정상'으로 학습된다. 결과는 models-ship.new 와
# data/ship/train.new.csv 에만 쓰고 돌고 있는 모델은 건드리지 않는다 — 학습 결과를 확인한 뒤
# models-ship 과 교체하고 tools/restart_ai.sh 를 실행한다.
set -euo pipefail
cd "$(dirname "$0")/.."
MIN=${1:-15}

.venv/bin/python -u tools/collect_live.py --batch --minutes "$MIN" \
  --url https://127.0.0.1:8443 --originator CAnchorAgent --ca certs/ship-ca.crt
f=$(ls -t data/live/*.jsonl | head -1)
.venv/bin/python tools/live_to_csv.py "$f" --last-min $((MIN + 5)) --out data/ship/train.new.csv
rm -rf models-ship.new
.venv/bin/python model_iforest.py train --profile ship \
  --csv data/ship/train.new.csv --models-dir models-ship.new
echo "REBASELINE_DONE"
