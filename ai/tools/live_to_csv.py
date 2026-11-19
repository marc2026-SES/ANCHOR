#!/usr/bin/env python3
"""수집 JSONL(collect_live.py) → 학습·리플레이용 CSV 변환.

실선박 CIN을 generator.py와 같은 CSV 스키마(ts, ship_id, subsystem, container,
values, label, anomaly_type)로 바꾼다. ESP32 주기가 흔들리므로(≈1.05s) 각 스트림을
명목 주기의 정규 격자에 선형 보간해 features의 윈도우 가정(등간격 샘플)을 만족시킨다.

- 품질 필터: climate ok!=true, gas warm!=true, position fix!=true 샘플 제외
- hatch는 이벤트 스트림이라 보간 없이 원시 시각 그대로 {"state": ...} 유지
- 라벨 없음(전부 0) — 실데이터는 정상 학습·라이브 추론용

사용: python tools/live_to_csv.py                      # data/live/*.jsonl 전부
     python tools/live_to_csv.py --out data/ship/train.csv
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

# sensor -> (명목 주기 s, 보간할 숫자 키, 품질 필터)
STREAMS: dict[str, tuple[int, list[str]]] = {
    "attitude": (1, ["roll", "pitch", "gx", "gy", "gz", "ax", "ay", "az"]),
    "vibration": (1, ["vib_rms_g", "vib_peak_g", "mean_g"]),
    "gas": (2, ["mq2_v"]),
    "climate": (5, ["temp_c", "rh"]),
    "position": (2, ["lat", "lon", "sog_kn", "cog"]),
}


def ct_epoch(ct) -> float | None:
    """ACME ct('20261118T061223,912922') → epoch 초."""
    if not isinstance(ct, str) or "T" not in ct:
        return None
    base, _, frac = ct.partition(",")
    try:
        dt = datetime.strptime(base, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return dt.timestamp() + (float("0." + frac) if frac else 0.0)


def quality_ok(sensor: str, con: dict) -> bool:
    if sensor == "climate":
        return con.get("ok") is True
    if sensor == "gas":
        return con.get("warm") is True
    if sensor == "position":
        return con.get("fix") is True
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="수집 JSONL -> 학습 CSV")
    ap.add_argument("inputs", nargs="*", default=None,
                    help="JSONL 파일들 (기본 data/live/*.jsonl)")
    ap.add_argument("--out", default=str(ROOT / "data" / "ship" / "train.csv"))
    ap.add_argument("--ship-id", default="ship-001")
    ap.add_argument("--last-min", type=float, default=None,
                    help="마지막 N분만 사용 — 백로그의 장기 공백(장비 꺼짐) 구간 배제")
    args = ap.parse_args()

    files = args.inputs or sorted(glob.glob(str(ROOT / "data" / "live" / "*.jsonl")))
    if not files:
        raise SystemExit("입력 JSONL 없음 — 먼저 tools/collect_live.py 로 수집하세요")

    # (device, sensor) -> [(장치 ts, CSE ct, con)] (ri 중복은 파일 간 자연 제거)
    raw: dict[tuple[str, str], list[tuple[float | None, float | None, dict]]] = {}
    seen_ri: set[str] = set()
    dropped_q = 0
    for f in files:
        for line in Path(f).read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            ri = str(r.get("ri"))
            if ri in seen_ri:
                continue
            seen_ri.add(ri)
            con = r.get("con") or {}
            sensor = r["sensor"]
            if not quality_ok(sensor, con):
                dropped_q += 1
                continue
            ts = con.get("ts")
            ts_s = float(ts) / 1000.0 if isinstance(ts, (int, float)) and ts > 0 else None
            ct_s = ct_epoch(r.get("ct"))
            if ts_s is None and ct_s is None:
                continue
            raw.setdefault((r["device"], sensor), []).append((ts_s, ct_s, con))

    # 장치 ts가 CSE 시각과 크게 어긋난 스트림(NTP 미동기)은 수신시각(ct)으로 대체
    streams: dict[tuple[str, str], dict[float, dict]] = {}
    for key, recs in raw.items():
        offs = sorted(t - c for t, c, _ in recs if t is not None and c is not None)
        use_ct = False
        if offs:
            med = offs[len(offs) // 2]
            if abs(med) > 300.0:
                use_ct = True
                print(f"[warn] {key[0]}/{key[1]}: 장치 ts가 CSE 시각과 {med / 3600.0:+.1f}h "
                      f"어긋남(NTP 미동기) — 수신시각(ct) 사용")
        by: dict[float, dict] = {}
        for t, c, con in recs:
            tt = (c if use_ct else t)
            if tt is None:
                tt = t if t is not None else c
            by[float(tt)] = con
        streams[key] = by

    if args.last_min and streams:
        t_max = max(max(v) for v in streams.values())
        cutoff = t_max - args.last_min * 60.0
        streams = {k: {ep: c for ep, c in v.items() if ep >= cutoff}
                   for k, v in streams.items()}
        streams = {k: v for k, v in streams.items() if v}
        print(f"--last-min {args.last_min:g}: {datetime.fromtimestamp(cutoff, tz=timezone.utc).strftime(TS_FMT)} 이후만 사용")

    # 모든 격자 스트림을 '공통 앵커'에 위상 정렬 — features의 윈도우 격자는 전역
    # t0 기준 등간격을 가정하므로, 스트림별 시작이 어긋나면 윈도우 계산이 깨진다.
    gridded = {k: np.array(sorted(v)) for k, v in streams.items()
               if k[1] in STREAMS and len(v) >= 4}
    if not gridded:
        raise SystemExit("격자화할 센서 스트림이 없음 (수집 데이터 확인)")
    anchor = float(np.ceil(max(t[0] for t in gridded.values())))
    end = float(np.floor(min(t[-1] for t in gridded.values())))
    if end - anchor < 2 * 60:
        print(f"[warn] 공통 구간이 {end - anchor:.0f}s 뿐 — 학습엔 부족 (수집을 더 하세요)")

    rows: list[tuple[float, str, str, str, dict]] = []  # (ep, ship, SUB, cont, values)
    print(f"입력 {len(files)}개 파일, 품질 필터 제외 {dropped_q}건, "
          f"공통 구간 {(end - anchor) / 60.0:.1f}분")
    for (device, sensor), by_ts in sorted(streams.items()):
        sub = device.upper()
        if sensor == "hatch":                      # 이벤트 스트림: 원시 시각 유지
            evs = [ep for ep in sorted(by_ts) if anchor <= ep <= end]
            for ep in evs:
                con = by_ts[ep]
                rows.append((ep, args.ship_id, sub, sensor,
                             {"state": str(con.get("state", "open")),
                              "open_s": con.get("open_s", 0)}))
            print(f"  {device}/{sensor:<10} 이벤트 {len(evs):,}건 (보간 없음)")
            continue
        if (device, sensor) not in gridded:
            continue
        t = gridded[(device, sensor)]
        step, keys = STREAMS[sensor]
        gaps = np.diff(t)
        grid = np.arange(anchor, end + 1e-9, step)
        vals = {k: np.interp(grid, t, np.array([float(by_ts[ep].get(k, 0.0)) for ep in t]))
                for k in keys}
        for i, ep in enumerate(grid):
            rows.append((float(ep), args.ship_id, sub, sensor,
                         {k: round(float(vals[k][i]), 6) for k in keys}))
        print(f"  {device}/{sensor:<10} {len(t):,}건 -> 격자 {len(grid):,}건 "
              f"(주기 {step}s, 원시 중앙 {np.median(gaps):.2f}s, 최대 공백 {gaps.max():.0f}s)")

    rows.sort(key=lambda r: r[0])
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        fh.write("ts,ship_id,subsystem,container,values,label,anomaly_type\n")
        for ep, ship, sub, cont, values in rows:
            iso = datetime.fromtimestamp(ep, tz=timezone.utc).strftime(TS_FMT)
            v = json.dumps(values, ensure_ascii=False, separators=(",", ":")).replace('"', '""')
            fh.write(f'{iso},{ship},{sub},{cont},"{v}",0,\n')
    span_min = (rows[-1][0] - rows[0][0]) / 60.0 if rows else 0.0
    print(f"-> {out} ({len(rows):,} rows, {span_min:.0f}분)")


if __name__ == "__main__":
    main()
