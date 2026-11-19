#!/usr/bin/env python3
"""고정 임계값 규칙 기준선.

config.yaml의 임계값으로 data/eval/eval.csv에서 이상을 탐지하고,
이상 유형별 precision/recall/F1(행 단위)과 탐지 지연 초(이벤트 단위)를
results/baseline.json에 저장한다. ML(Isolation Forest)과 비교하기 위한 기준.

규칙 ↔ 이상 유형 매핑 (유형별 대상 스트림이 1:1이라 유형별 지표 산출 가능)
  spike        ENGINE/gas          ppm > gas_ppm_max
  drift        ENGINE/temperature  temp > engine_temp_max_c
  level_shift  ENGINE/vibration    rms > vib_rms_max
  freq_change  BRIDGE/attitude     roll 주기(zero-crossing 추정)가 [min,max] 밖
  cargo_temp   CARGO/climate       temp > cargo_temp_max_c
  hatch_open   CARGO/hatch         state==open 이고 sog > hatch_sog_min_kn (position 결합)

사용: python baseline_rule.py [--config config.yaml]
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent

RULE_TARGETS = {
    "spike": ("ENGINE", "gas"),
    "drift": ("ENGINE", "temperature"),
    "level_shift": ("ENGINE", "vibration"),
    "freq_change": ("BRIDGE", "attitude"),
    "cargo_temp": ("CARGO", "climate"),
    "hatch_open": ("CARGO", "hatch"),
}


def get_stream(df: pd.DataFrame, sub: str, cont: str) -> pd.DataFrame:
    """스트림 추출. reset_index()의 'index' 컬럼이 eval.csv 전체 행 인덱스."""
    s = df[(df["subsystem"] == sub) & (df["container"] == cont)].copy()
    return s.sort_values("t").reset_index()


def col(s: pd.DataFrame, key: str) -> np.ndarray:
    return s["vals"].map(lambda v: float(v[key])).to_numpy()


def roll_period_alarm(roll: np.ndarray, thr: dict) -> np.ndarray:
    """1Hz roll 신호에서 zero-crossing으로 주기를 추정해 대역 밖이면 알람.

    3점 이동평균으로 노이즈를 줄인 뒤 부호 변화 횟수를 윈도우(roll_window_s)에서
    세고, 주기 ≈ 2*W / 횟수 로 추정한다.
    """
    w = int(thr["roll_window_s"])
    sm = pd.Series(roll).rolling(3, center=True, min_periods=1).mean().to_numpy()
    sign = np.where(sm >= 0.0, 1, -1)
    cross = np.zeros(sm.shape[0])
    cross[1:] = (sign[1:] != sign[:-1]).astype(float)
    ccount = pd.Series(cross).rolling(w, min_periods=w).sum().to_numpy()
    period = np.where(ccount >= 4, 2.0 * w / np.maximum(ccount, 1.0), np.nan)
    alarm = (period < thr["roll_period_min_s"]) | (period > thr["roll_period_max_s"])
    return np.where(np.isnan(period), False, alarm).astype(bool)


def detect_all(df: pd.DataFrame, thr: dict):
    """스트림별 규칙 적용. 반환: {type: 스트림 df}, {type: 행 단위 알람 bool 배열}."""
    streams: dict[str, pd.DataFrame] = {}
    preds: dict[str, np.ndarray] = {}

    g = get_stream(df, "ENGINE", "gas")
    streams["spike"], preds["spike"] = g, col(g, "ppm") > thr["gas_ppm_max"]

    t = get_stream(df, "ENGINE", "temperature")
    streams["drift"], preds["drift"] = t, col(t, "temp") > thr["engine_temp_max_c"]

    v = get_stream(df, "ENGINE", "vibration")
    streams["level_shift"], preds["level_shift"] = v, col(v, "rms") > thr["vib_rms_max"]

    a = get_stream(df, "BRIDGE", "attitude")
    streams["freq_change"], preds["freq_change"] = a, roll_period_alarm(col(a, "roll"), thr)

    c = get_stream(df, "CARGO", "climate")
    streams["cargo_temp"], preds["cargo_temp"] = c, col(c, "temp") > thr["cargo_temp_max_c"]

    h = get_stream(df, "CARGO", "hatch")
    streams["hatch_open"] = h
    if len(h):
        p = get_stream(df, "BRIDGE", "position")
        pos = pd.DataFrame({"t": p["t"], "sog": col(p, "sog")})
        merged = pd.merge_asof(h[["t"]], pos, on="t", direction="backward")
        state = h["vals"].map(lambda x: x["state"]).to_numpy()
        sog = np.nan_to_num(merged["sog"].to_numpy())
        preds["hatch_open"] = (state == "open") & (sog > thr["hatch_sog_min_kn"])
    else:
        preds["hatch_open"] = np.zeros(0, dtype=bool)
    return streams, preds


def prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return precision, recall, f1


def main() -> None:
    ap = argparse.ArgumentParser(description="고정 임계값 규칙 기준선 평가")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    thr = cfg["thresholds"]
    paths = cfg.get("paths", {})
    eval_csv = ROOT / paths.get("eval_csv", "data/eval/eval.csv")
    events_json = ROOT / paths.get("events_json", "data/eval/events.json")
    results_json = ROOT / paths.get("results_json", "results/baseline.json")

    if not eval_csv.exists():
        raise SystemExit(f"{eval_csv} 없음 — 먼저 `python generator.py` 를 실행하세요")

    df = pd.read_csv(eval_csv, keep_default_na=False)
    df["label"] = df["label"].astype(int)
    df["t"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(None)  # naive UTC로 통일
    df["vals"] = df["values"].map(json.loads)
    events = json.loads(events_json.read_text(encoding="utf-8"))["events"] if events_json.exists() else []

    streams, preds = detect_all(df, thr)

    pred_full = np.zeros(len(df), dtype=bool)
    per_type: dict[str, dict] = {}
    all_latencies: list[float] = []
    total_detected = 0

    for typ in RULE_TARGETS:
        s, yp = streams[typ], preds[typ]
        if len(s):
            pred_full[s["index"].to_numpy()[yp]] = True

        # 행 단위 P/R/F1 (해당 스트림에서 알람 vs 라벨)
        yt = (s["label"].to_numpy() == 1) & (s["anomaly_type"].to_numpy() == typ) if len(s) \
            else np.zeros(0, dtype=bool)
        tp = int(np.sum(yt & yp)); fp = int(np.sum(~yt & yp)); fn = int(np.sum(yt & ~yp))
        precision, recall, f1 = prf(tp, fp, fn)

        # 이벤트 단위 탐지율·지연 (events.json 기준)
        evs = [e for e in events if e["type"] == typ]
        lats: list[float] = []
        detected = 0
        tm = s["t"].to_numpy() if len(s) else np.array([], dtype="datetime64[ns]")
        for e in evs:
            t0 = pd.Timestamp(e["start"]).tz_convert(None).to_datetime64()
            t1 = pd.Timestamp(e["end"]).tz_convert(None).to_datetime64()
            m = yp & (tm >= t0) & (tm <= t1)
            idx = np.flatnonzero(m)
            if idx.size:
                detected += 1
                lats.append(float((tm[idx[0]] - t0) / np.timedelta64(1, "s")))
        total_detected += detected
        all_latencies += lats

        per_type[typ] = {
            "stream": f"{RULE_TARGETS[typ][0]}/{RULE_TARGETS[typ][1]}",
            "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
            "tp": tp, "fp": fp, "fn": fn,
            "events_total": len(evs), "events_detected": detected,
            "detection_latency_s": {
                "mean": round(float(np.mean(lats)), 1) if lats else None,
                "median": round(float(np.median(lats)), 1) if lats else None,
                "per_event": [round(x, 1) for x in lats],
            },
        }

    y_full = df["label"].to_numpy() == 1
    tp = int(np.sum(y_full & pred_full)); fp = int(np.sum(~y_full & pred_full)); fn = int(np.sum(y_full & ~pred_full))
    precision, recall, f1 = prf(tp, fp, fn)
    overall = {
        "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
        "tp": tp, "fp": fp, "fn": fn, "rows": int(len(df)),
        "events_total": len(events), "events_detected": total_detected,
        "detection_latency_s": {
            "mean": round(float(np.mean(all_latencies)), 1) if all_latencies else None,
            "median": round(float(np.median(all_latencies)), 1) if all_latencies else None,
        },
    }

    results = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "eval_csv": str(eval_csv.relative_to(ROOT)),
        "events_json": str(events_json.relative_to(ROOT)),
        "thresholds": thr,
        "per_type": per_type,
        "overall": overall,
    }
    results_json.parent.mkdir(parents=True, exist_ok=True)
    results_json.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"{'type':<12} {'P':>6} {'R':>6} {'F1':>6}  {'events':>8}  latency(mean s)")
    for typ, m in per_type.items():
        lat = m["detection_latency_s"]["mean"]
        print(f"{typ:<12} {m['precision']:>6.3f} {m['recall']:>6.3f} {m['f1']:>6.3f}  "
              f"{m['events_detected']}/{m['events_total']:>2}       {lat if lat is not None else '-'}")
    print(f"{'overall':<12} {overall['precision']:>6.3f} {overall['recall']:>6.3f} {overall['f1']:>6.3f}  "
          f"{overall['events_detected']}/{overall['events_total']:>2}       "
          f"{overall['detection_latency_s']['mean']}")
    print(f"-> {results_json.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
