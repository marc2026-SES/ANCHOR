#!/usr/bin/env python3
"""서브시스템별 Isolation Forest 학습·점수화.

- 학습(정상 데이터만, CLAUDE.md 규칙): data/train/train.csv 의 윈도우 특징으로
  ENGINE/BRIDGE/CARGO 각각 IsolationForest를 학습한다. 시간순 앞 80%로 fit,
  뒤 20%는 검증(validation)으로 남겨 점수 정규화와 임계값을 정한다.
- 점수 정규화(0~1): raw = -score_samples 로 클수록 이상.
  score01 = clip((raw - median_val) / (3 × (q99.5_val - median_val)), 0, 1)
  → warn = 1/3 은 "검증 정상의 상위 0.5% 지점", critical = 2/3 은 그 2배 거리.
  (임계값을 검증 데이터 분위수로 정하되 라벨은 쓰지 않는다)
- 근거(topFeatures): fit 구간 특징 분포 기준 z-score 절대값 상위 3개.
  출력 스키마는 CLAUDE.md anomaly flexContainer
  {subsystem, level, score, topFeatures, windowStart, windowEnd, srcContainers} 대응.
- 저장: models/iforest_<SUBSYSTEM>.joblib, models/model_card.json

사용
  python model_iforest.py                                           # 학습
  python model_iforest.py score --csv data/eval/eval.csv --out results/scores_eval.csv
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import IsolationForest

import features
from features import (SRC_CONTAINERS, STRIDE_S, SUBSYSTEMS, WINDOW_S,
                      feature_cols, load_csv, subsystem_features)

ROOT = Path(__file__).resolve().parent
MODELS_DIR = ROOT / "models"
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

WARN_T = 1.0 / 3.0       # 정규화 점수 경보 임계값 (검증 정상 상위 0.5% 지점)
CRITICAL_T = 2.0 / 3.0

N_ESTIMATORS = 150       # RPi5 윈도우당 50ms 예산 내 (이 머신 기준 ~7ms)
MAX_SAMPLES = 4096       # 서브샘플이 클수록 범위 밖 이상 대비가 좋았음 (README 참고)
AUG_FRAC = 0.05          # 경계 증강 비율
AUG_ALPHA = 1.0          # 특징 범위 확장 배수


def _augment_boundary(x_fit: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """경계 증강: 특징 박스를 α×range 넓힌 균일 합성점을 frac 비율만큼 추가.

    IsolationForest의 분할값은 학습 데이터 범위 안에서만 생기므로, 범위 밖
    이상치의 경로 길이는 '학습 최대/최소점의 깊이'에서 포화된다(범위 밖 둔감).
    소량의 균일 배경점을 섞어 범위 밖에도 분할을 만들면 명백한 범위 이탈이
    바로 고립된다. 검증 분위수와 z-score 통계는 실제 정상 데이터로만 계산한다.
    """
    lo, hi = x_fit.min(axis=0), x_fit.max(axis=0)
    span = np.maximum(hi - lo, 1e-9)
    n = max(int(len(x_fit) * AUG_FRAC), 1)
    synth = rng.uniform(lo - AUG_ALPHA * span, hi + AUG_ALPHA * span,
                        size=(n, x_fit.shape[1]))
    return np.vstack([x_fit, synth])


def model_path(subsystem: str) -> Path:
    return MODELS_DIR / f"iforest_{subsystem}.joblib"


def normalize(bundle: dict, raw: np.ndarray) -> np.ndarray:
    return np.clip((raw - bundle["norm_median"]) / (3.0 * bundle["norm_span"]), 0.0, 1.0)


def train(seed: int = 42, val_frac: float = 0.2, csv_path: str | Path | None = None,
          models_dir: str | Path | None = None, profile: str | None = None) -> None:
    global MODELS_DIR
    if profile:
        features.set_profile(profile)
    if models_dir:
        MODELS_DIR = Path(models_dir)
        if not MODELS_DIR.is_absolute():
            MODELS_DIR = ROOT / MODELS_DIR
    train_csv = Path(csv_path) if csv_path else ROOT / "data" / "train" / "train.csv"
    if not train_csv.exists():
        raise SystemExit(f"{train_csv} 없음 — 먼저 `python generator.py` 를 실행하세요")
    df = load_csv(train_csv)
    assert int(df["label"].sum()) == 0, "train에 라벨이 있으면 안 됨"
    MODELS_DIR.mkdir(exist_ok=True)

    card: dict = {
        "trained_at": datetime.now(timezone.utc).strftime(TS_FMT),
        "profile": features.ACTIVE_PROFILE, "train_csv": str(train_csv),
        "window_s": WINDOW_S, "stride_s": STRIDE_S,
        "n_estimators": N_ESTIMATORS, "max_samples": MAX_SAMPLES,
        "boundary_aug": {"frac": AUG_FRAC, "alpha": AUG_ALPHA},
        "seed": seed, "val_frac": val_frac,
        "warn_threshold": round(WARN_T, 4), "critical_threshold": round(CRITICAL_T, 4),
        "sklearn": sklearn.__version__,
        "subsystems": {},
    }
    for sub in SUBSYSTEMS:
        fdf = subsystem_features(df, sub)
        cols = feature_cols(fdf)
        X = fdf[cols].to_numpy(dtype=float)
        n_fit = int(len(X) * (1.0 - val_frac))
        X_fit, X_val = X[:n_fit], X[n_fit:]

        X_aug = _augment_boundary(X_fit, np.random.default_rng(seed))
        model = IsolationForest(n_estimators=N_ESTIMATORS,
                                max_samples=min(MAX_SAMPLES, len(X_aug)),
                                random_state=seed, n_jobs=-1).fit(X_aug)
        raw_val = -model.score_samples(X_val)
        med = float(np.median(raw_val))
        q995 = float(np.quantile(raw_val, 0.995))
        span = max(q995 - med, 1e-9)

        # 단일 윈도우 추론 시간 (RPi5 예산 50ms 참고치, 이 머신 기준)
        x1 = X_val[:1]
        tic = time.perf_counter()
        for _ in range(50):
            model.score_samples(x1)
        ms = (time.perf_counter() - tic) / 50 * 1000.0

        bundle = {
            "subsystem": sub, "model": model, "feature_names": cols,
            "feat_mean": X_fit.mean(axis=0), "feat_std": np.maximum(X_fit.std(axis=0), 1e-9),
            "norm_median": med, "norm_span": span,
            "warn": WARN_T, "critical": CRITICAL_T,
            "window_s": WINDOW_S, "stride_s": STRIDE_S, "seed": seed,
        }
        joblib.dump(bundle, model_path(sub))

        val01 = normalize(bundle, raw_val)
        card["subsystems"][sub] = {
            "n_features": len(cols),
            "n_windows_fit": int(n_fit), "n_windows_val": int(len(X_val)),
            "raw_median_val": round(med, 4), "raw_q995_val": round(q995, 4),
            "warn_raw": round(med + span, 4), "critical_raw": round(med + 2 * span, 4),
            "val_fpr_at_warn": round(float((val01 >= WARN_T).mean()), 5),
            "score_ms_per_window": round(ms, 2),
            "model_file": model_path(sub).name,
        }
        print(f"{sub:<6} features={len(cols):>2} fit={n_fit} val={len(X_val)} "
              f"warn_raw={med + span:.4f} | {ms:.1f} ms/window")

    (MODELS_DIR / "model_card.json").write_text(
        json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"-> {MODELS_DIR}/iforest_*.joblib, model_card.json")


def load_models(models_dir: str | Path | None = None) -> dict[str, dict]:
    d = Path(models_dir) if models_dir else MODELS_DIR
    out = {}
    for sub in SUBSYSTEMS:
        p = d / f"iforest_{sub}.joblib"
        if not p.exists():
            raise SystemExit(f"{p} 없음 — 먼저 `python model_iforest.py` 로 학습하세요")
        out[sub] = joblib.load(p)
    return out


def score_array(bundle: dict, X: np.ndarray):
    """특징 행렬(n×d, feature_names 순서) → (score01, level, topFeatures 리스트).

    실시간(analytics_ae.py)에서는 n=1 로 호출한다.
    """
    cols = bundle["feature_names"]
    raw = -bundle["model"].score_samples(X)
    s01 = normalize(bundle, raw)
    level = np.where(s01 >= bundle["critical"], "critical",
                     np.where(s01 >= bundle["warn"], "warn", "normal"))
    z = (X - bundle["feat_mean"]) / bundle["feat_std"]
    order = np.argsort(-np.abs(z), axis=1)[:, :3]
    top = [[{"feature": cols[j], "z": round(float(z[i, j]), 2)} for j in order[i]]
           for i in range(X.shape[0])]
    return s01, level, top


def score_windows(bundle: dict, fdf: pd.DataFrame) -> pd.DataFrame:
    """윈도우 특징 → score(0~1)/level/topFeatures. 메타(label 등)는 평가용으로 유지."""
    X = fdf[bundle["feature_names"]].to_numpy(dtype=float)
    s01, level, top = score_array(bundle, X)

    out = fdf[["window_start", "window_end", "label", "anomaly_type"]].copy()
    out.insert(0, "subsystem", bundle["subsystem"])
    out["score"] = np.round(s01, 4)
    out["level"] = level
    out["top_features"] = [json.dumps(t, ensure_ascii=False) for t in top]
    out["src_containers"] = json.dumps(SRC_CONTAINERS[bundle["subsystem"]])
    return out


def to_anomaly_payload(row) -> dict:
    """스코어 행 → CLAUDE.md anomaly flexContainer 형태의 dict."""
    top = row["top_features"]
    src = row["src_containers"]
    return {
        "subsystem": row["subsystem"],
        "level": str(row["level"]),
        "score": float(row["score"]),
        "topFeatures": json.loads(top) if isinstance(top, str) else top,
        "windowStart": pd.Timestamp(row["window_start"]).strftime(TS_FMT),
        "windowEnd": pd.Timestamp(row["window_end"]).strftime(TS_FMT),
        "srcContainers": json.loads(src) if isinstance(src, str) else src,
    }


def score_csv(csv_path: str, out_path: str) -> None:
    df = load_csv(csv_path)
    models = load_models()
    parts = [score_windows(models[sub], subsystem_features(df, sub)) for sub in SUBSYSTEMS]
    scored = pd.concat(parts, ignore_index=True).sort_values(
        ["window_start", "subsystem"], ignore_index=True)
    for c in ("window_start", "window_end"):
        scored[c] = scored[c].dt.strftime(TS_FMT)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    scored.to_csv(out, index=False)
    counts = scored["level"].value_counts().to_dict()
    print(f"{len(scored)} windows -> {out}  levels={counts}")


def main() -> None:
    ap = argparse.ArgumentParser(description="서브시스템별 Isolation Forest (상세: README.md)")
    sp = ap.add_subparsers(dest="cmd")
    tr = sp.add_parser("train", help="train 데이터로 학습 (기본 동작)")
    tr.add_argument("--seed", type=int, default=42)
    tr.add_argument("--val-frac", type=float, default=0.2)
    tr.add_argument("--csv", default=None, help="학습 CSV (기본 data/train/train.csv)")
    tr.add_argument("--models-dir", default=None, help="모델 저장 디렉터리 (기본 models/)")
    tr.add_argument("--profile", default=None, help="센서 프로필 (synthetic|ship)")
    sc = sp.add_parser("score", help="CSV를 점수화해 윈도우 스코어 CSV 저장")
    sc.add_argument("--csv", default=str(ROOT / "data" / "eval" / "eval.csv"))
    sc.add_argument("--out", default=str(ROOT / "results" / "scores_eval.csv"))
    args = ap.parse_args()

    if args.cmd == "score":
        score_csv(args.csv, args.out)
    else:
        train(seed=getattr(args, "seed", 42), val_frac=getattr(args, "val_frac", 0.2),
              csv_path=getattr(args, "csv", None),
              models_dir=getattr(args, "models_dir", None),
              profile=getattr(args, "profile", None))


if __name__ == "__main__":
    main()
