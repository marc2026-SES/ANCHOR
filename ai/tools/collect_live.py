#!/usr/bin/env python3
"""실선박 CSE에서 센서 CIN을 읽기 전용으로 수집한다 (실데이터 재학습용 원본).

팀 Tailscale로 선박 Pi(ACME MN-CSE, cse-ship)의 센서 컨테이너를 폴링해 JSONL로
저장한다. CSE에는 아무 리소스도 만들지 않는다 — 방식은 컨테이너별 CIN 경로
디스커버리(fu=1) 후 처음 보는 경로만 개별 RETRIEVE (이 CSE는 cra/rcn=4 자식
포함이 동작하지 않아 경로 차집합으로 새 CIN을 찾는다).

기본은 CSE에 이미 쌓인 백로그까지 전부 수집한다 (책상 위 정상 데이터 = 학습감).
--from-now 를 주면 시작 시점 이후 새 CIN만 수집한다.

사용: python tools/collect_live.py --minutes 30
출력: data/live/live_<UTC시각>.jsonl — 한 줄에 CIN 하나:
  {"device", "sensor", "ri", "ct", "seq", "con": {원본 con 그대로}}
끝에 센서별 건수·데이터 시간 범위·장치별 seq 공백(노드 공유 순번 기준)을 요약.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from onem2m_client import OneM2MClient, OneM2MError  # noqa: E402

# (device, sensor) — ANCHOR hardware-overview.md 의 선박 트리
SENSORS: list[tuple[str, str]] = [
    ("bridge", "attitude"), ("bridge", "position"),
    ("engine", "vibration"), ("engine", "gas"),
    ("cargo", "climate"), ("cargo", "hatch"),
]


def main() -> None:
    ap = argparse.ArgumentParser(description="선박 CSE 센서 CIN 수집기 (읽기 전용)")
    ap.add_argument("--url", default="http://100.114.83.111:8080",
                    help="선박 Pi CSE 주소 (팀 Tailscale)")
    ap.add_argument("--cse-rn", default="cse-ship")
    ap.add_argument("--originator", default="CAdmin")
    ap.add_argument("--rvi", default="3")
    ap.add_argument("--minutes", type=float, default=30.0, help="수집 길이(분)")
    ap.add_argument("--interval", type=float, default=6.0, help="폴링 간격(초)")
    ap.add_argument("--from-now", action="store_true",
                    help="백로그 생략 — 시작 이후 새 CIN만")
    ap.add_argument("--out-dir", default=str(ROOT / "data" / "live"))
    args = ap.parse_args()

    client = OneM2MClient(args.url, args.cse_rn, args.originator,
                          rvi=args.rvi, timeout=15.0)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"live_{stamp}.jsonl"
    out = out_path.open("w", encoding="utf-8")

    seen: dict[tuple[str, str], set[str]] = {k: set() for k in SENSORS}
    counts = {k: 0 for k in SENSORS}
    dev_seqs: dict[str, set[int]] = {}
    ts_range: dict[tuple[str, str], list[float]] = {}

    def fetch_new(key: tuple[str, str]) -> int:
        path = f"{key[0]}/{key[1]}"
        uril = client.discover_cins(path)
        new = [u for u in uril if u not in seen[key]]
        got = 0
        for u in new:
            res = client.retrieve(u)
            if res is None:
                seen[key].add(u)          # 사라진 CIN(만료 등)은 건너뜀
                continue
            cin = res.get("m2m:cin") or {}
            con = cin.get("con")
            try:
                data = json.loads(con) if isinstance(con, str) else dict(con or {})
            except ValueError:
                data = {"_raw": con}
            seq = data.get("seq")
            if isinstance(seq, int):
                dev_seqs.setdefault(key[0], set()).add(seq)
            ts = data.get("ts")
            if isinstance(ts, (int, float)) and ts > 0:
                r = ts_range.setdefault(key, [ts, ts])
                r[0], r[1] = min(r[0], ts), max(r[1], ts)
            out.write(json.dumps({"device": key[0], "sensor": key[1],
                                  "ri": cin.get("ri"), "ct": cin.get("ct"),
                                  "seq": seq, "con": data}, ensure_ascii=False) + "\n")
            seen[key].add(u)
            counts[key] += 1
            got += 1
        return got

    if args.from_now:                      # 기존 경로를 '본 것'으로만 표시
        for key in SENSORS:
            try:
                seen[key].update(client.discover_cins(f"{key[0]}/{key[1]}"))
            except (OneM2MError, OSError):
                pass
        backlog = 0
    else:
        print("백로그 수집 중 (CSE에 쌓여 있던 분량)…")
        backlog = 0
        for key in SENSORS:
            try:
                n = fetch_new(key)
                backlog += n
                print(f"  {key[0]}/{key[1]}: {n:,}건")
            except (OneM2MError, OSError) as e:
                print(f"[warn] {key[0]}/{key[1]} 백로그 실패: {e}")
        out.flush()
    print(f"수집 시작 -> {out_path.relative_to(ROOT)} "
          f"(백로그 {backlog:,}건, 이후 {args.minutes:g}분 라이브)")

    t_end = time.monotonic() + args.minutes * 60.0
    last_status = time.monotonic()
    while time.monotonic() < t_end:
        tic = time.monotonic()
        for key in SENSORS:
            try:
                fetch_new(key)
            except (OneM2MError, OSError) as e:
                print(f"[warn] {key[0]}/{key[1]} 조회 실패: {e} — 다음 라운드 재시도")
        out.flush()
        if time.monotonic() - last_status >= 60.0:
            last_status = time.monotonic()
            left = max(0.0, (t_end - time.monotonic()) / 60.0)
            print(f"  {sum(counts.values()):,}건 (남은 {left:.0f}분) "
                  + " ".join(f"{k[1]}={v:,}" for k, v in counts.items()))
        time.sleep(max(0.0, args.interval - (time.monotonic() - tic)))
    out.close()

    print(f"\n수집 완료: {sum(counts.values()):,}건 -> {out_path.relative_to(ROOT)}")
    for key in SENSORS:
        line = f"  {key[0]}/{key[1]:<10} {counts[key]:,}건"
        if key in ts_range:
            a, b = ts_range[key]
            line += f", 데이터 범위 {(b - a) / 60000.0:.0f}분"
        print(line)
    for dev, seqs in sorted(dev_seqs.items()):
        if len(seqs) >= 2:
            holes = (max(seqs) - min(seqs) + 1) - len(seqs)
            print(f"  seq 공백 {dev}: {holes} (노드 공유 순번 {min(seqs)}~{max(seqs)})")


if __name__ == "__main__":
    main()
