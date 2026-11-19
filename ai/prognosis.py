#!/usr/bin/env python3
"""고장 예지: 센서 추세를 투영해 임계값 도달 시점을 미리 알린다.

규칙 기준선(baseline_rule.py)이 "임계값을 넘었다"를, IF가 "패턴이 평소와 다르다"를
알린다면, 여기는 "이 추세면 N분 뒤 임계값에 도달한다"를 알린다 — 탐지 위의 예지 층.
대상 채널과 임계값은 config.yaml `prognosis.channels`가 `thresholds` 섹션을 참조한다.

방법: 채널별 최근 trend_min 분 샘플에 최소제곱 직선을 맞춰 기울기를 얻고,
현재 추정값(절편)에서 임계값까지 ETA를 계산한다. ETA가 [min_eta_s, warn_min]이면
threshold_predicted 1회 발화, 추세가 꺾여 ETA가 1.5×warn_min 밖으로 나가면 재무장.
급격한 spike는 min_eta_s에 걸러진다 — 그건 규칙/IF 몫.

이벤트 (분석 AE가 anchorAI/prognosis 컨테이너에 CIN으로 기록):
  threshold_predicted  {channel, threshold, value, slopePerMin, etaS, eventId}

단독 시뮬: python prognosis.py   # data/eval/eval.csv 재생
          → 이벤트 타임라인 출력 + results/prognosis_events.jsonl
"""
from __future__ import annotations

import json
from collections import deque
from pathlib import Path

import numpy as np

from link_forecast import event_id

ROOT = Path(__file__).resolve().parent
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


class TrendPrognoser:
    """observe()로 샘플을 받아 임계값 도달 예측 이벤트를 on_event(dict)로 알린다."""

    def __init__(self, cfg: dict, thresholds: dict, on_event):
        self.trend_s = int(cfg.get("trend_min", 5)) * 60
        self.warn_s = int(cfg.get("warn_min", 10)) * 60
        self.min_eta_s = float(cfg.get("min_eta_s", 60))
        self.interval_s = float(cfg.get("interval_s", 15))
        self.on_event = on_event
        self.ch: dict[tuple[str, str, str], dict] = {}
        for c in cfg.get("channels", []):
            key = (str(c["subsystem"]), str(c["container"]), str(c["key"]))
            self.ch[key] = {"limit": float(thresholds[str(c["threshold"])]),
                            "thr": str(c["threshold"]),
                            "buf": deque(), "warned": False, "last_ep": -1e18}

    def observe(self, subsystem: str, container: str, values: dict,
                ep: float, iso_ts: str) -> None:
        for (sub, cont, vkey), st in self.ch.items():
            if (sub, cont) != (subsystem, container) or vkey not in values:
                continue
            buf = st["buf"]
            buf.append((ep, float(values[vkey])))
            while buf and buf[0][0] < ep - self.trend_s:
                buf.popleft()
            if ep - st["last_ep"] < self.interval_s:
                continue
            st["last_ep"] = ep
            self._evaluate(f"{sub}/{cont}.{vkey}", st, ep, iso_ts)

    def _evaluate(self, channel: str, st: dict, ep: float, iso_ts: str) -> None:
        buf = st["buf"]
        if len(buf) < 8 or buf[-1][0] - buf[0][0] < 0.6 * self.trend_s:
            return
        t = np.array([s[0] - ep for s in buf])
        v = np.array([s[1] for s in buf])
        slope, icpt = np.polyfit(t, v, 1)           # 초당 변화율
        cur = float(icpt)                           # t=0(현재) 추정값 — 잡음 평활
        limit = st["limit"]
        if cur >= limit:                            # 이미 초과 — 규칙/IF 영역
            st["warned"] = True
            return
        eta = (limit - cur) / float(slope) if slope > 1e-12 else None
        if not st["warned"] and eta is not None and self.min_eta_s <= eta <= self.warn_s:
            st["warned"] = True
            self.on_event({"type": "threshold_predicted", "ts": iso_ts,
                           "eventId": event_id("prg", iso_ts, channel.split("/")[-1]),
                           "channel": channel, "thresholdKey": st["thr"],
                           "threshold": limit, "value": round(cur, 3),
                           "slopePerMin": round(float(slope) * 60.0, 4),
                           "etaS": int(eta)})
        elif st["warned"] and (eta is None or eta > 1.5 * self.warn_s):
            st["warned"] = False                    # 추세 완화 — 재무장


# ---------------------------------------------------------------- 단독 시뮬

def replay(csv_path: str | Path, cfg: dict, thresholds: dict) -> list[dict]:
    """eval.csv의 대상 채널 행을 시간순으로 흘려 이벤트 타임라인 생성."""
    from datetime import datetime, timezone

    import pandas as pd

    want = {(str(c["subsystem"]), str(c["container"])) for c in cfg.get("channels", [])}
    df = pd.read_csv(csv_path, keep_default_na=False)
    df = df[[(s, c) in want for s, c in zip(df["subsystem"], df["container"])]]
    events: list[dict] = []

    def on_event(ev: dict) -> None:
        events.append(ev)
        print(f"{ev['ts']} {ev['type']:<19} {ev['channel']:<26} eta={ev['etaS']}s "
              f"({ev['value']} -> {ev['threshold']}, {ev['slopePerMin']:+.3f}/min)")

    pg = TrendPrognoser(cfg, thresholds, on_event)
    for r in df.itertuples(index=False):
        ep = datetime.strptime(r.ts, TS_FMT).replace(tzinfo=timezone.utc).timestamp()
        pg.observe(r.subsystem, r.container, json.loads(r.values), ep, r.ts)
    return events


if __name__ == "__main__":
    import argparse

    import yaml

    ap = argparse.ArgumentParser(description="고장 예지 단독 시뮬 (상세: README.md)")
    ap.add_argument("--csv", default=str(ROOT / "data" / "eval" / "eval.csv"))
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    args = ap.parse_args()
    cfg_all = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    events = replay(args.csv, cfg_all["prognosis"], cfg_all["thresholds"])
    out = ROOT / "results" / "prognosis_events.jsonl"
    out.parent.mkdir(exist_ok=True)
    out.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
                   encoding="utf-8")
    print(f"{len(events)} events -> {out.relative_to(ROOT)}")
