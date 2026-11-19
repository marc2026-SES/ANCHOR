#!/usr/bin/env python3
"""ANCHOR 연합학습: Federated Isolation Forest (FL_PLAN.md 3장 구현).

가중치 평균(FedAvg)은 Isolation Forest에 맞지 않으므로 트리 병합을 쓴다.
특징 추출(30s 창, 5s 이동)과 점수 정규화는 model_iforest.py 그대로.

함수 (코디네이터·참가자에서 그대로 가져다 쓴다):
  local_train(df, subsystem, seed, ...) -> update  참가자 로컬 학습 (트리 + n + 검증 분포)
  merge(updates, target_trees)          -> model   트리를 n_i 비례로 선발해 전역 포레스트
  calibrate(model, updates)             -> (norm_median, norm_span)  가중 평균 보정
  evaluate(bundle, fdf)                 -> dict    공통 평가셋 precision/recall/F1
  to_bundle(...)                        -> dict    model_iforest.py bundle 형식 (analytics_ae 호환)

검증:
  python fed_iforest.py selftest             sklearn 내부 배열 병합 무결성 (계획서 10장 리스크)
  python fed_iforest.py simulate --hours 2   oneM2M 없이 로컬 단독/FL/중앙 3자 비교 (H1-H4)

주의: 모든 참가자가 같은 max_samples를 써야 점수 척도가 맞는다. merge가 강제로 확인한다.
"""
from __future__ import annotations

import argparse
import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

import joblib
import numpy as np
import sklearn
from sklearn.ensemble import IsolationForest

from features import SUBSYSTEMS, WINDOW_S, STRIDE_S, feature_cols, load_csv, subsystem_features
from model_iforest import WARN_T, CRITICAL_T, normalize

ROOT = Path(__file__).resolve().parent
TREES_PER_CLIENT = 20
FL_MAX_SAMPLES = 256
GLOBAL_TREES = 150


# ── 참가자 쪽 ────────────────────────────────────────────────────────────────
def local_train(df, subsystem: str, seed: int, trees: int = TREES_PER_CLIENT,
                max_samples: int = FL_MAX_SAMPLES, val_frac: float = 0.2) -> dict:
    """정상 데이터만으로 로컬 포레스트를 학습한다. 원본 데이터는 update에 담지 않는다."""
    fdf = subsystem_features(df, subsystem)
    cols = feature_cols(fdf)
    X = fdf[cols].to_numpy(dtype=float)
    n_fit = int(len(X) * (1.0 - val_frac))
    X_fit, X_val = X[:n_fit], X[n_fit:]
    model = IsolationForest(n_estimators=trees, max_samples=min(max_samples, len(X_fit)),
                            random_state=seed, n_jobs=-1).fit(X_fit)
    return {"model": model, "n": int(n_fit), "cols": cols,
            "feat_mean": X_fit.mean(axis=0), "feat_std": np.maximum(X_fit.std(axis=0), 1e-9),
            "X_val": X_val}  # X_val은 보정 때 로컬에서만 쓰고 업로드하지 않는다


def update_payload(u: dict) -> bytes:
    """업로드 본문: 트리와 통계만 직렬화 (joblib). 로컬 검증 데이터는 제외."""
    import io
    buf = io.BytesIO()
    joblib.dump({k: u[k] for k in ("model", "n", "cols", "feat_mean", "feat_std")}, buf, compress=3)
    return buf.getvalue()


# ── 코디네이터 쪽 ────────────────────────────────────────────────────────────
def merge(updates: list[dict], target_trees: int = GLOBAL_TREES, seed: int = 0) -> IsolationForest:
    """참가자 트리를 n_i에 비례해 골라 전역 포레스트를 만든다 (sklearn 1.7.2 기준, selftest로 확인)."""
    models = [u["model"] for u in updates]
    ms = {int(m.max_samples_) for m in models}
    if len(ms) != 1:
        raise ValueError(f"max_samples가 달라 점수 척도가 안 맞음: {ms}")
    if len({m.n_features_in_ for m in models}) != 1:
        raise ValueError("참가자끼리 특징 수가 다름")
    ns = np.array([u["n"] for u in updates], dtype=float)
    # 가용 트리보다 큰 목표는 같은 트리를 복제해 추출 노이즈만 늘린다 (시뮬 실측: ENGINE F1 0.38->0.47)
    target_trees = min(target_trees, sum(len(u["model"].estimators_) for u in updates))
    quota = ns / ns.sum() * target_trees
    counts = np.floor(quota).astype(int)
    for i in np.argsort(-(quota - counts))[: target_trees - counts.sum()]:
        counts[i] += 1
    counts = np.maximum(counts, 1)  # 선택된 참가자는 최소 1그루 기여

    rng = np.random.default_rng(seed)
    g = copy.deepcopy(models[0])
    # sklearn 1.7.x는 트리 리스트 외에 트리별 캐시 2개(_average_path_length_per_tree,
    # _decision_path_lengths)를 fit에서 만들어 score_samples가 인덱스를 맞춰 쓴다.
    # 셋을 같은 순서로 함께 모으지 않으면 IndexError 또는 조용히 틀린 점수가 난다 (selftest가 검증).
    est, feats, apl, dpl = [], [], [], []
    for m, k in zip(models, counts):
        have = len(m.estimators_)
        idx = rng.choice(have, size=int(k), replace=int(k) > have)
        for i in idx:
            est.append(m.estimators_[i])
            feats.append(m.estimators_features_[i])
            if hasattr(m, "_average_path_length_per_tree"):
                apl.append(m._average_path_length_per_tree[i])
                dpl.append(m._decision_path_lengths[i])
    g.estimators_ = est
    g.estimators_features_ = feats
    g.n_estimators = len(est)
    if apl:
        g._average_path_length_per_tree = tuple(apl)
        g._decision_path_lengths = tuple(dpl)
    return g


def calibrate(model: IsolationForest, updates: list[dict]) -> tuple[float, float]:
    """전역 모델을 각 참가자 로컬 검증 창에 적용해 분포(중앙값, 99.5% 분위수)를 모으고
    n_i 가중 평균으로 norm_median/norm_span을 정한다. (배포에서는 분포 숫자만 업로드된다)"""
    w = np.array([u["n"] for u in updates], dtype=float)
    w /= w.sum()
    meds, spans = [], []
    for u in updates:
        raw = -model.score_samples(u["X_val"])
        med = float(np.median(raw))
        meds.append(med)
        spans.append(max(float(np.quantile(raw, 0.995)) - med, 1e-9))
    return float(np.dot(w, meds)), float(np.dot(w, spans))


def to_bundle(subsystem: str, model, cols, feat_mean, feat_std,
              norm_median: float, norm_span: float, seed: int = 0) -> dict:
    """model_iforest.py bundle과 같은 키 — joblib으로 저장하면 analytics_ae가 그대로 쓴다."""
    return {"subsystem": subsystem, "model": model, "feature_names": cols,
            "feat_mean": feat_mean, "feat_std": feat_std,
            "norm_median": norm_median, "norm_span": norm_span,
            "warn": WARN_T, "critical": CRITICAL_T,
            "window_s": WINDOW_S, "stride_s": STRIDE_S, "seed": seed}


def evaluate(bundle: dict, fdf) -> dict:
    X = fdf[bundle["feature_names"]].to_numpy(dtype=float)
    s01 = normalize(bundle, -bundle["model"].score_samples(X))
    pred = s01 >= bundle["warn"]
    y = fdf["label"].to_numpy() == 1
    tp = int((pred & y).sum()); fp = int((pred & ~y).sum()); fn = int((~pred & y).sum())
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return {"precision": round(prec, 3), "recall": round(rec, 3), "f1": round(f1, 3),
            "tp": tp, "fp": fp, "fn": fn, "windows": int(len(y)), "positives": int(y.sum())}


# ── 검증 1: 병합 무결성 (계획서 10장 리스크) ─────────────────────────────────
def selftest() -> None:
    rng = np.random.default_rng(0)
    X = rng.normal(size=(600, 7))
    m = IsolationForest(n_estimators=24, max_samples=256, random_state=1).fit(X)
    whole = merge([{"model": m, "n": 600}], target_trees=24, seed=0)
    # 한 모델의 트리 '전부'를 병합한 결과는 원본과 점수가 같아야 한다 (같은 트리 집합이므로)
    a, b = np.sort(m.score_samples(X)), np.sort(whole.score_samples(X))
    assert np.allclose(a, b, atol=1e-12), "병합 포레스트 점수가 원본과 다름 — sklearn 내부 구조 변경 의심"
    # 서로 다른 데이터의 두 모델 병합이 유한한 점수를 내는지
    m2 = IsolationForest(n_estimators=24, max_samples=256, random_state=2).fit(X * 3 + 1)
    g = merge([{"model": m, "n": 600}, {"model": m2, "n": 600}], target_trees=30, seed=0)
    s = g.score_samples(X)
    assert np.isfinite(s).all() and len(g.estimators_) == 30
    print(f"selftest ok (sklearn {sklearn.__version__}): whole-merge identical, cross-merge finite, 30 trees")


# ── 검증 2: 로컬 시뮬레이션 — 로컬 단독 vs FL vs 중앙 (H1–H4 완료 기준) ──────
# generator.py가 --profile을 지원하기 전까지의 대역: 시드를 바꾸고 센서 값을
# 프로필별 배율로 스케일해 non-IID를 흉내 낸다. AI 파트가 2장 프로필을 넣으면
# PROFILE_SCALE만 지우고 같은 비교를 다시 돌리면 된다.
PROFILE_SCALE = {
    "ferry":     {"vibration": 1.6, "attitude": 1.0},
    "fishing":   {"vibration": 0.7, "attitude": 1.8},
    "container": {"vibration": 1.0, "attitude": 0.6},
}


def _scaled(df, profile: str):
    sc = PROFILE_SCALE[profile]
    df = df.copy()
    def f(row):
        k = sc.get(row["container"])
        if not k:
            return row["vals"]
        return {key: (v * k if isinstance(v, (int, float)) and not isinstance(v, bool) else v)
                for key, v in row["vals"].items()}
    df["vals"] = df.apply(f, axis=1)
    return df


def _gen_client(name: str, seed: int, hours: float) -> Path:
    out = ROOT / "data" / "fl" / name
    if (out / "train.csv").exists() and (out / "eval.csv").exists():
        return out
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, str(ROOT / "generator.py"),
                    "--hours", str(hours), "--seed", str(seed)], check=True, cwd=ROOT)
    shutil.copy(ROOT / "data" / "train" / "train.csv", out / "train.csv")
    shutil.copy(ROOT / "data" / "eval" / "eval.csv", out / "eval.csv")
    return out


def simulate(hours: float = 2.0) -> None:
    clients = [("ferry-hakata", "ferry", 11), ("fishing-02", "fishing", 12),
               ("container-01", "container", 13)]
    data = {}
    for name, prof, seed in clients:
        d = _gen_client(name, seed, hours)
        data[name] = (prof, seed,
                      _scaled(load_csv(d / "train.csv"), prof),
                      _scaled(load_csv(d / "eval.csv"), prof))
    report = {}
    for sub in SUBSYSTEMS:
        ups = {n: local_train(tr, sub, seed) for n, (p, seed, tr, ev) in data.items()}
        evals = {n: subsystem_features(ev, sub) for n, (p, s, tr, ev) in data.items()}
        import pandas as pd
        common = pd.concat(evals.values(), ignore_index=True)

        # 로컬 단독: 자기 보정으로, 자기 평가셋 / 전체 평가셋
        own_f1, all_f1 = [], []
        for n, u in ups.items():
            med, span = calibrate(u["model"], [u])
            b = to_bundle(sub, u["model"], u["cols"], u["feat_mean"], u["feat_std"], med, span)
            own_f1.append(evaluate(b, evals[n])["f1"])
            all_f1.append(evaluate(b, common)["f1"])

        # FL: 트리 병합 + 전원 보정
        ul = list(ups.values())
        g = merge(ul, GLOBAL_TREES)
        med, span = calibrate(g, ul)
        fl = evaluate(to_bundle(sub, g, ul[0]["cols"], ul[0]["feat_mean"], ul[0]["feat_std"], med, span), common)

        # 중앙 집중(상한선): 모든 데이터를 한곳에 모았다고 치고 학습
        allfit = pd.concat([subsystem_features(tr, sub) for n, (p, s, tr, ev) in data.items()],
                           ignore_index=True)
        cols = ul[0]["cols"]
        Xc = allfit[cols].to_numpy(dtype=float)
        cm = IsolationForest(n_estimators=GLOBAL_TREES, max_samples=FL_MAX_SAMPLES,
                             random_state=0, n_jobs=-1).fit(Xc)
        cu = {"model": cm, "n": len(Xc), "X_val": Xc[int(len(Xc) * 0.8):]}
        med, span = calibrate(cm, [cu])
        ct = evaluate(to_bundle(sub, cm, cols, Xc.mean(axis=0), np.maximum(Xc.std(axis=0), 1e-9),
                                med, span), common)

        report[sub] = {"local_own_f1_avg": round(float(np.mean(own_f1)), 3),
                       "local_all_f1_avg": round(float(np.mean(all_f1)), 3),
                       "fl_all": fl, "central_all": ct}
        print(f"{sub:<6} local(own) F1={report[sub]['local_own_f1_avg']:.3f}  "
              f"local(all) F1={report[sub]['local_all_f1_avg']:.3f}  "
              f"FL(all) F1={fl['f1']:.3f}  central(all) F1={ct['f1']:.3f}")
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results" / "fl_simulation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2))
    print("-> results/fl_simulation.json")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["selftest", "simulate"])
    ap.add_argument("--hours", type=float, default=2.0)
    a = ap.parse_args()
    selftest() if a.cmd == "selftest" else simulate(a.hours)
