#!/usr/bin/env python3
"""클라우드 학습 → oneM2M 모델 배포 (모델 배포 루프의 '발행' 쪽).

model_iforest로 재학습한 뒤 models 디렉터리를 tar.gz+base64로 묶어
레지스트리 CSE의 anchorAI/modelRegistry 컨테이너에 CIN 한 건으로 발행한다.
엣지 분석 AE(analytics_ae)는 이 컨테이너를 감시하다 새 버전을 해시 검증 후
무중단으로 끼운다 — "클라우드 학습 → 엣지 추론 → 모델 배포" 루프의 완성.

사용:
  python tools/train_publish.py                      # 재학습 + 발행
  python tools/train_publish.py --skip-train         # 현재 모델만 발행
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from onem2m_client import OneM2MClient  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="모델 재학습 + oneM2M 레지스트리 발행")
    ap.add_argument("--csv", default=str(ROOT / "data" / "ship" / "train.csv"))
    ap.add_argument("--profile", default="ship")
    ap.add_argument("--models-dir", default="models-ship")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--skip-train", action="store_true", help="학습 생략, 현재 모델 발행만")
    ap.add_argument("--registry-url", default="http://100.96.155.4:30081",
                    help="레지스트리 CSE (기본: 팀 육상 IN-CSE)")
    ap.add_argument("--cse-rn", default="cse-in")
    ap.add_argument("--originator", default="CAnchorAgent")
    ap.add_argument("--rvi", default="3")
    ap.add_argument("--container", default="modelRegistry")
    args = ap.parse_args()

    if not args.skip_train:
        import model_iforest
        print(f"== 재학습 ({args.profile}, {args.csv}) ==")
        model_iforest.train(seed=args.seed, csv_path=args.csv,
                            models_dir=args.models_dir, profile=args.profile)

    mdir = Path(args.models_dir)
    if not mdir.is_absolute():
        mdir = ROOT / mdir
    files = sorted(p for p in mdir.iterdir()
                   if p.name.endswith(".joblib") or p.name == "model_card.json")
    if not files:
        raise SystemExit(f"{mdir}에 모델 파일 없음")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for p in files:
            tf.add(p, arcname=p.name)
    blob = buf.getvalue()
    sha = hashlib.sha256(blob).hexdigest()
    version = "v" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    client = OneM2MClient(args.registry_url, args.cse_rn, args.originator,
                          rvi=args.rvi, timeout=30.0)
    client.ensure_ae("anchorAI", "NanchorAI")
    client.ensure_container("anchorAI", args.container, mni=20)
    payload = {"version": version, "profile": args.profile,
               "trainedAt": version[1:], "sha256": sha, "sizeB": len(blob),
               "files": [p.name for p in files], "encoding": "tar.gz+base64",
               "eventId": f"mdl-{version}",
               "data": base64.b64encode(blob).decode("ascii")}
    client.create_cin(f"anchorAI/{args.container}", payload, lbl=["model", args.profile])
    print(f"발행 완료: {version} ({args.profile}) -> {args.registry_url}/{args.cse_rn}"
          f"/anchorAI/{args.container}  tar {len(blob):,}B / b64 {len(payload['data']):,}B")


if __name__ == "__main__":
    main()
