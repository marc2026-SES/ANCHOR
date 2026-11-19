#!/usr/bin/env python3
"""Isolation Forest vs 규칙 기준선 비교 평가.

- data/eval/eval.csv 를 윈도우 특징으로 점수화(warn 이상 = 알람)해
  유형별 P/R/F1(윈도우 단위)과 이벤트 탐지 지연(초)을 계산
- results/baseline.json 의 규칙 기준선과 비교 표 출력 + drift 지연 단축(초) 명시
- 유형별 '시계열 + 점수 + 탐지 시점' 그래프를 results/plot_<type>.png 로 저장
- 수치는 results/evaluation.json 저장

주의: 규칙 기준선의 P/R/F1은 행 단위, IF는 30s 윈도우 단위라 절대값은 참고용.
이벤트 탐지율과 탐지 지연(초)이 직접 비교 지표다. IF의 탐지 시각은
'윈도우 끝'(판정 가능 시점) 기준이라 윈도우 길이만큼의 비용이 포함된다.

사용: python evaluate.py
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from baseline_rule import prf
from features import SUBSYSTEMS, load_csv, subsystem_features
from model_iforest import CRITICAL_T, TS_FMT, WARN_T, load_models, score_windows

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"

TYPE_SUBSYSTEM = {
    "spike": "ENGINE", "drift": "ENGINE", "level_shift": "ENGINE",
    "freq_change": "BRIDGE", "cargo_temp": "CARGO", "hatch_open": "CARGO",
}
# 그래프 상단에 그릴 원시 시계열 (hatch_open은 open_frac 특징으로 대체)
PLOT_SERIES = {
    "spike": ("ENGINE", "gas", "ppm"),
    "drift": ("ENGINE", "temperature", "temp"),
    "level_shift": ("ENGINE", "vibration", "rms"),
    "freq_change": ("BRIDGE", "attitude", "roll"),
    "cargo_temp": ("CARGO", "climate", "temp"),
    "hatch_open": None,
}


def ev_window(e: dict) -> tuple[pd.Timestamp, pd.Timestamp]:
    return (pd.Timestamp(e["start"]).tz_convert(None),
            pd.Timestamp(e["end"]).tz_convert(None))


def event_detection(s: pd.DataFrame, alarm: np.ndarray, e: dict):
    """이벤트와 겹치는 첫 알람 윈도우. 반환 (detected, latency_s, 탐지시각=윈도우 끝)."""
    t0, t1 = ev_window(e)
    ws = s["window_start"].to_numpy()
    we = s["window_end"].to_numpy()
    m = alarm & (we > t0.to_datetime64()) & (ws < t1.to_datetime64())
    idx = np.flatnonzero(m)
    if not idx.size:
        return False, None, None
    dt = pd.Timestamp(we[idx[0]])
    return True, max(0.0, float((dt - t0).total_seconds())), dt


def plot_type(typ: str, e: dict, df: pd.DataFrame, scored: dict, fdfs: dict,
              baseline: dict, n_events: int) -> Path:
    sub = TYPE_SUBSYSTEM[typ]
    s = scored[sub]
    alarm = (s["level"] != "normal").to_numpy()
    t0, t1 = ev_window(e)
    pad = pd.Timedelta(seconds=max(300.0, 0.5 * (t1 - t0).total_seconds()))
    lo, hi = t0 - pad, t1 + pad

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6), sharex=True,
                                   gridspec_kw={"height_ratios": [2, 1]})
    if PLOT_SERIES[typ] is not None:
        psub, cont, key = PLOT_SERIES[typ]
        raw = df[(df["subsystem"] == psub) & (df["container"] == cont)]
        m = (raw["t"] >= lo) & (raw["t"] <= hi)
        ax1.plot(raw.loc[m, "t"], raw.loc[m, "vals"].map(lambda d: d[key]),
                 lw=0.8, color="#1f77b4")
        ax1.set_ylabel(f"{psub}/{cont}.{key}")
    else:
        fdf = fdfs[sub]
        m = (fdf["window_end"] >= lo) & (fdf["window_end"] <= hi)
        ax1.step(fdf.loc[m, "window_end"], fdf.loc[m, "hatch.open_frac"],
                 where="post", color="#1f77b4")
        ax1.set_ylabel("hatch open_frac")
        ax1.set_ylim(-0.05, 1.1)
    ax1.axvspan(t0, t1, color="red", alpha=0.12)
    ax1.set_title(f"{typ}  ({e['start']} ~ {e['end']})")

    m2 = (s["window_end"] >= lo) & (s["window_end"] <= hi)
    ax2.plot(s.loc[m2, "window_end"], s.loc[m2, "score"], lw=1.0,
             color="#d62728", label="IF score")
    ax2.axhline(WARN_T, ls="--", lw=0.8, color="#ff7f0e")
    ax2.axhline(CRITICAL_T, ls="--", lw=0.8, color="#d62728")
    ax2.axvspan(t0, t1, color="red", alpha=0.12)
    ax2.set_ylim(-0.02, 1.05)
    ax2.set_ylabel("score (0-1)")

    det, lat, dt = event_detection(s, alarm, e)
    if det:
        ax2.axvline(dt, color="#d62728", lw=1.2, label=f"IF detect +{lat:.0f}s")
    rule_lats = baseline["per_type"][typ]["detection_latency_s"]["per_event"]
    if len(rule_lats) == n_events and rule_lats:  # 전부 탐지됐을 때만 1:1 대응
        rt = t0 + pd.Timedelta(seconds=rule_lats[0])
        ax2.axvline(rt, color="#7f7f7f", ls=":", lw=1.4, label=f"rule detect +{rule_lats[0]:.0f}s")
    ax2.legend(loc="upper right", fontsize=8)
    fig.autofmt_xdate()
    fig.tight_layout()
    out = RESULTS / f"plot_{typ}.png"
    fig.savefig(out, dpi=120)
    plt.close(fig)
    return out


def main() -> None:
    eval_csv = ROOT / "data" / "eval" / "eval.csv"
    events_json = ROOT / "data" / "eval" / "events.json"
    baseline_json = RESULTS / "baseline.json"
    if not eval_csv.exists():
        raise SystemExit(f"{eval_csv} 없음 — 먼저 `python generator.py` 를 실행하세요")
    if not baseline_json.exists():
        raise SystemExit(f"{baseline_json} 없음 — 먼저 `python baseline_rule.py` 를 실행하세요")

    df = load_csv(eval_csv)
    events = json.loads(events_json.read_text(encoding="utf-8"))["events"]
    baseline = json.loads(baseline_json.read_text(encoding="utf-8"))
    models = load_models()

    fdfs = {sub: subsystem_features(df, sub) for sub in SUBSYSTEMS}
    scored = {sub: score_windows(models[sub], fdfs[sub]) for sub in SUBSYSTEMS}

    per_type: dict[str, dict] = {}
    all_lats: list[float] = []
    total_detected = 0
    for typ, sub in TYPE_SUBSYSTEM.items():
        s = scored[sub]
        alarm = (s["level"] != "normal").to_numpy()
        at = s["anomaly_type"].to_numpy()
        # 같은 서브시스템의 다른 유형 윈도우는 제외하고 정상 vs 해당 유형으로 평가
        mask = (at == "") | (at == typ)
        yt = (at == typ)[mask]
        yp = alarm[mask]
        tp = int(np.sum(yt & yp)); fp = int(np.sum(~yt & yp)); fn = int(np.sum(yt & ~yp))
        precision, recall, f1 = prf(tp, fp, fn)

        evs = [e for e in events if e["type"] == typ]
        lats: list[float] = []
        detected = 0
        for e in evs:
            det, lat, _ = event_detection(s, alarm, e)
            if det:
                detected += 1
                lats.append(lat)
        total_detected += detected
        all_lats += lats

        per_type[typ] = {
            "subsystem": sub,
            "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
            "tp": tp, "fp": fp, "fn": fn,
            "events_total": len(evs), "events_detected": detected,
            "detection_latency_s": {
                "mean": round(float(np.mean(lats)), 1) if lats else None,
                "median": round(float(np.median(lats)), 1) if lats else None,
                "per_event": [round(x, 1) for x in lats],
            },
        }

    # 전체(micro): 모든 서브시스템 윈도우
    y_all = np.concatenate([scored[sub]["label"].to_numpy() == 1 for sub in SUBSYSTEMS])
    p_all = np.concatenate([(scored[sub]["level"] != "normal").to_numpy() for sub in SUBSYSTEMS])
    tp = int(np.sum(y_all & p_all)); fp = int(np.sum(~y_all & p_all)); fn = int(np.sum(y_all & ~p_all))
    precision, recall, f1 = prf(tp, fp, fn)
    overall = {
        "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
        "tp": tp, "fp": fp, "fn": fn, "windows": int(y_all.shape[0]),
        "events_total": len(events), "events_detected": total_detected,
        "detection_latency_s": {
            "mean": round(float(np.mean(all_lats)), 1) if all_lats else None,
            "median": round(float(np.median(all_lats)), 1) if all_lats else None,
        },
    }

    # 규칙 기준선과 비교
    comparison: dict[str, dict] = {}
    for typ in TYPE_SUBSYSTEM:
        b = baseline["per_type"][typ]
        m = per_type[typ]
        b_lat = b["detection_latency_s"]["mean"]
        m_lat = m["detection_latency_s"]["mean"]
        comparison[typ] = {
            "f1_rule": b["f1"], "f1_iforest": m["f1"],
            "f1_delta": round(m["f1"] - b["f1"], 4),
            "latency_rule_s": b_lat, "latency_iforest_s": m_lat,
            "latency_reduction_s": round(b_lat - m_lat, 1)
            if (b_lat is not None and m_lat is not None) else None,
            "events_rule": f"{b['events_detected']}/{b['events_total']}",
            "events_iforest": f"{m['events_detected']}/{m['events_total']}",
        }
    drift_improvement = comparison["drift"]["latency_reduction_s"]

    # 그래프 (유형별 첫 이벤트)
    RESULTS.mkdir(exist_ok=True)
    plots: list[str] = []
    for typ in TYPE_SUBSYSTEM:
        evs = [e for e in events if e["type"] == typ]
        if evs:
            p = plot_type(typ, evs[0], df, scored, fdfs, baseline, len(evs))
            plots.append(str(p.relative_to(ROOT)))

    results = {
        "generated_at": datetime.now(timezone.utc).strftime(TS_FMT),
        "alarm": f"score >= warn({WARN_T:.3f})",
        "note": "rule 지표는 행 단위, IF 지표는 30s 윈도우 단위(탐지시각=윈도우 끝)",
        "iforest": {"per_type": per_type, "overall": overall},
        "comparison": comparison,
        "drift_latency_improvement_s": drift_improvement,
        "plots": plots,
    }
    out = RESULTS / "evaluation.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    # 표 출력
    print(f"{'type':<12} {'F1 rule':>8} {'F1 IF':>7} {'dF1':>7}   "
          f"{'lat rule':>8} {'lat IF':>7} {'saved':>7}   events(IF)")
    for typ, c in comparison.items():
        br = f"{c['latency_rule_s']:.0f}s" if c["latency_rule_s"] is not None else "-"
        mi = f"{c['latency_iforest_s']:.0f}s" if c["latency_iforest_s"] is not None else "-"
        dl = f"{c['latency_reduction_s']:+.0f}s" if c["latency_reduction_s"] is not None else "-"
        print(f"{typ:<12} {c['f1_rule']:>8.3f} {c['f1_iforest']:>7.3f} {c['f1_delta']:>+7.3f}   "
              f"{br:>8} {mi:>7} {dl:>7}   {c['events_iforest']}")
    o = overall
    print(f"{'overall':<12} {baseline['overall']['f1']:>8.3f} {o['f1']:>7.3f} "
          f"{o['f1'] - baseline['overall']['f1']:>+7.3f}   "
          f"{baseline['overall']['detection_latency_s']['mean']:>7.0f}s "
          f"{o['detection_latency_s']['mean']:>6.0f}s"
          f"{'':>8}   {o['events_detected']}/{o['events_total']}")
    if drift_improvement is not None:
        print(f"\ndrift 탐지 지연: 규칙 {comparison['drift']['latency_rule_s']:.0f}s → "
              f"IF {comparison['drift']['latency_iforest_s']:.0f}s "
              f"({drift_improvement:+.0f}s 단축)")
    print(f"plots: {', '.join(plots)}")
    print(f"-> {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
