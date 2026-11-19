#!/usr/bin/env python3
"""센서 시뮬레이터 (라이브 데모·테스트용).

generator.py가 만든 CSV를 oneM2M CIN으로 CSE에 주입한다.
- config.yaml의 onem2m.sensors 경로대로 센서 AE(SHIP-01)와 컨테이너 트리를 만들고(재사용),
  분석 AE originator에 권한을 주는 ACP를 걸어둔다
  (subscription 생성에는 대상 컨테이너에 대한 권한이 필요).
- CSV 행을 ts 순서대로 CIN CREATE → analytics_ae.py가 구독 notification으로 받아 추론한다.

사용 예 (drift 이벤트 구간):
  python tools/feed_csv.py --from-ts 2026-01-01T12:14:00Z --minutes 8
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from features import load_csv                      # noqa: E402
from onem2m_client import ACOP_ALL, OneM2MClient, OneM2MError  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="CSV -> oneM2M CIN 주입기")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--csv", default=str(ROOT / "data" / "eval" / "eval.csv"))
    ap.add_argument("--from-ts", default=None, help="시작 시각 (예: 2026-01-01T12:14:00Z)")
    ap.add_argument("--minutes", type=float, default=10.0, help="주입 길이(분)")
    ap.add_argument("--originator", default="CSHIP01", help="센서 AE originator ('C…')")
    ap.add_argument("--api", default="NsensorSim")
    ap.add_argument("--sleep-ms", type=float, default=0.0, help="CIN 사이 대기(ms)")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    o = cfg["onem2m"]
    client = OneM2MClient(o["cse_url"], o["cse_rn"], args.originator, rvi=str(o.get("rvi", "4")))
    analytics_origin = o["ae"]["originator"]

    sensors = [p.strip("/") for p in o["sensors"]]
    ae_rn = sensors[0].split("/")[0]
    ae_path = f"{o['cse_rn']}/{ae_rn}"

    # AE + ACP + 컨테이너 트리 (재사용)
    _, created = client.ensure_ae(ae_rn, args.api)
    print(f"sensor AE {ae_rn}: {'등록' if created else '재사용'}")
    acp, _ = client.ensure_acp(ae_path, "acpAnchor",
                               grants=[([analytics_origin, args.originator], ACOP_ALL)],
                               self_originators=[args.originator])
    acp_ri = str(client.resource_attr(acp, "ri"))
    actuators = [m.strip("/") for m in (o.get("actuators") or {}).get("members", [])]
    for path in sensors + actuators:               # 액추에이터(alarm 노드)도 같은 ACP로 준비
        parts = path.split("/")                    # SHIP-01/ENGINE/gas
        parent = ae_path
        for depth, seg in enumerate(parts[1:]):
            mni = (200 if path in sensors else 50) if depth == len(parts) - 2 else None
            res, was_created = client.ensure_container(parent, seg, mni=mni, acpi=[acp_ri])
            if not was_created and not client.resource_attr(res, "acpi"):
                client.update(f"{parent}/{seg}", {"m2m:cnt": {"acpi": [acp_ri]}})
            parent = f"{parent}/{seg}"
    print(f"컨테이너 트리 준비 완료 (ACP {acp_ri}, 센서 {len(sensors)} + 액추에이터 {len(actuators)})")

    # CSV 로드·필터
    df = load_csv(args.csv)
    targets = {(p.split("/")[-2], p.split("/")[-1]): p for p in sensors}
    df = df[[(s, c) in targets for s, c in zip(df["subsystem"], df["container"])]]
    df = df.sort_values("t")
    t0 = pd.Timestamp(args.from_ts).tz_convert(None) if args.from_ts else df["t"].min()
    t1 = t0 + pd.Timedelta(minutes=args.minutes)
    df = df[(df["t"] >= t0) & (df["t"] < t1)]
    print(f"{len(df):,} rows 주입 ({t0} ~ {t1})")

    sent = errors = 0
    tic = time.perf_counter()
    for r in df.itertuples(index=False):
        path = targets[(r.subsystem, r.container)]
        con = json.dumps({"ts": r.ts, **r.vals}, ensure_ascii=False, separators=(",", ":"))
        try:
            client.create_cin(f"{o['cse_rn']}/{path}", con,
                              lbl=[str(r.ship_id), r.subsystem, r.container])
            sent += 1
        except OneM2MError as e:
            errors += 1
            if errors <= 3:
                print(f"[warn] CIN 실패 {path}: {e}")
        if args.sleep_ms:
            time.sleep(args.sleep_ms / 1000.0)
        if sent % 500 == 0 and sent:
            print(f"  {sent:,} CIN 전송…")
    dur = time.perf_counter() - tic
    print(f"완료: {sent:,} CIN, 실패 {errors}, {dur:.1f}s ({sent / max(dur, 1e-9):.0f} CIN/s)")


if __name__ == "__main__":
    main()
