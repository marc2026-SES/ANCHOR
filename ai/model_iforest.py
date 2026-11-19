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
from features import (SRC_CONTAINERS, SUBSYSTEMS,
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
TOL_COPIES = 8           # 허용오차 증강 복제 수 (프로필에 tolerance가 있을 때만)
TRIM_K = 3.0             # 충격 윈도우 제외 기준: 원점수 중앙값 + K×MAD (프로필 trim일 때만).
                         # 5였을 땐 2~3분마다 건드린 흔적이 남아 ENGINE 기준이 도달 불가로 올라갔다
TRIM_MAD_FLOOR = 0.01    # 점수가 거의 상수인 서브시스템에서 기준이 0으로 붙는 것 방지


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


def _tolerance_jitter(X: np.ndarray, cols: list[str], rng: np.random.Generator) -> np.ndarray:
    """허용오차 증강: 센서 정확도 범위의 변동을 정상으로 학습시킨다 (프로필 tolerance).

    눈금이 거친 센서(DHT11 0.1도/1%)는 정상 구간 내내 값이 사실상 상수라 std·slope가
    정확히 0으로만 학습되고, 한 눈금만 오가도 '본 적 없는 값'으로 critical이 된다.
    관측 분산이 0일 때 믿을 수 있는 정상 폭은 센서 정확도뿐이므로, 원본 윈도우마다
    수준 이동(±tol)과 윈도우 내 변동(±tol/4)을 mean/std/rms/max/slope에 물리적으로
    일관되게 반영한 복제본 TOL_COPIES개를 덧붙인다. tolerance가 없으면 그대로 반환.
    """
    idx = {c: i for i, c in enumerate(cols)}
    targets = [(f"{cont}.{key}", t) for (cont, key), t in features.TOLERANCE.items()
               if f"{cont}.{key}.mean" in idx]
    if not targets:
        return X
    out = [X]
    n = len(X)
    for _ in range(TOL_COPIES):
        J = X.copy()
        for p, tol in targets:
            w = tol / 4.0
            shift = rng.uniform(-tol, tol, n)        # 수준 이동
            drift = rng.uniform(-w, w, n)            # 윈도우 안에서 움직인 양
            noise = rng.uniform(0.0, w / 2.0, n)     # 눈금 오가기(샘플 잡음) 크기
            mean = X[:, idx[f"{p}.mean"]] + shift
            std = np.sqrt(X[:, idx[f"{p}.std"]] ** 2 + drift ** 2 / 12.0 + noise ** 2)
            J[:, idx[f"{p}.mean"]] = mean
            J[:, idx[f"{p}.std"]] = std
            J[:, idx[f"{p}.rms"]] = np.sqrt(mean ** 2 + std ** 2)
            J[:, idx[f"{p}.max"]] = X[:, idx[f"{p}.max"]] + shift + np.abs(drift) / 2.0 + noise
            J[:, idx[f"{p}.slope"]] = X[:, idx[f"{p}.slope"]] + drift / features.WINDOW_S
        out.append(J)
    return np.vstack(out)


def _trim_mask(X: np.ndarray, cols: list[str], seed: int) -> np.ndarray:
    """정상 수집분에 섞인 순간 충격 윈도우를 가려낸다 (프로필 trim, 실데이터용).

    전체 윈도우로 한 번 적합한 뒤 원점수가 중앙값 + TRIM_K×MAD(정규 환산)를 넘는 윈도우를
    제외한다. 반환은 남길 윈도우의 불리언 마스크.
    """
    Xj = _tolerance_jitter(X, cols, np.random.default_rng(seed + 1))
    probe = IsolationForest(n_estimators=N_ESTIMATORS, max_samples=min(MAX_SAMPLES, len(Xj)),
                            random_state=seed, n_jobs=-1).fit(Xj)
    raw = -probe.score_samples(X)
    med = float(np.median(raw))
    mad = 1.4826 * float(np.median(np.abs(raw - med)))
    return raw <= med + TRIM_K * max(mad, TRIM_MAD_FLOOR)


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
        "window_s": features.WINDOW_S, "stride_s": features.STRIDE_S,
        "n_estimators": N_ESTIMATORS, "max_samples": MAX_SAMPLES,
        "boundary_aug": {"frac": AUG_FRAC, "alpha": AUG_ALPHA},
        "tolerance_aug": {"copies": TOL_COPIES,
                          "tolerance": {f"{c}.{k}": t for (c, k), t in features.TOLERANCE.items()}},
        "seed": seed, "val_frac": val_frac,
        "warn_threshold": round(WARN_T, 4), "critical_threshold": round(CRITICAL_T, 4),
        "sklearn": sklearn.__version__,
        "subsystems": {},
    }
    for sub in SUBSYSTEMS:
        fdf = subsystem_features(df, sub)
        cols = feature_cols(fdf)
        X = fdf[cols].to_numpy(dtype=float)
        n_trimmed = 0
        if features.TRIM_OUTLIERS:
            keep = _trim_mask(X, cols, seed)
            n_trimmed = int((~keep).sum())
            X = X[keep]
        n_fit = int(len(X) * (1.0 - val_frac))
        tol_rng = np.random.default_rng(seed + 1)
        X_fit = _tolerance_jitter(X[:n_fit], cols, tol_rng)
        X_val = _tolerance_jitter(X[n_fit:], cols, tol_rng)

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
            "window_s": features.WINDOW_S, "stride_s": features.STRIDE_S, "seed": seed,
        }
        joblib.dump(bundle, model_path(sub))

        val01 = normalize(bundle, raw_val)
        card["subsystems"][sub] = {
            "n_features": len(cols),
            "n_windows_fit": int(n_fit), "n_windows_val": int(len(X) - n_fit),
            "n_windows_trimmed": n_trimmed,
            "raw_median_val": round(med, 4), "raw_q995_val": round(q995, 4),
            "warn_raw": round(med + span, 4), "critical_raw": round(med + 2 * span, 4),
            "val_fpr_at_warn": round(float((val01 >= WARN_T).mean()), 5),
            "score_ms_per_window": round(ms, 2),
            "model_file": model_path(sub).name,
        }
        print(f"{sub:<6} features={len(cols):>2} fit={n_fit} val={len(X) - n_fit} "
              f"trimmed={n_trimmed} warn_raw={med + span:.4f} | {ms:.1f} ms/window")

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
