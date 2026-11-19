#!/usr/bin/env python3
"""윈도우 특징 추출.

generator.py가 만든 CSV를 30초 슬라이딩 윈도우(5초 stride)로 잘라
서브시스템별(ENGINE/BRIDGE/CARGO) 특징 행렬을 만든다.

- 센서별 특징: mean, std, rms, max, slope(초당 변화율, 최소제곱)
- vibration.rms, attitude.roll 은 FFT 주요 주파수(domfreq, Hz) 추가
- CARGO 는 hatch 개방 비율(open_frac: 윈도우 중 open 상태 비율) 추가
- 윈도우 라벨: 윈도우 [start, start+30s) 안에 해당 서브시스템의 라벨된 행이
  하나라도 있으면 label=1, anomaly_type은 그 행의 유형

RPi5 추론을 고려해 전부 numpy 벡터 연산이며, 1개 윈도우 특징은 동일한 식을
실시간 버퍼에 적용해 계산할 수 있다.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent

WINDOW_S = 30
STRIDE_S = 5

# 센서 프로필 2종: synthetic(합성 데모·평가, CLAUDE.md 구성) / ship(실선박 ESP32 구성 —
# ANCHOR hardware-overview.md: ENGINE 온도 센서 없음, gas는 MQ-2 전압 2s, climate 5s).
# set_profile()이 아래 전역 dict/set을 '제자리에서' 교체해 from-import 참조를 유지한다.
PROFILES: dict[str, dict] = {
    "synthetic": {
        "sensors": {   # subsystem -> [(container, 값 키, 샘플 주기 s)]
            "ENGINE": [("vibration", "rms", 1), ("temperature", "temp", 5), ("gas", "ppm", 5)],
            "BRIDGE": [("attitude", "roll", 1), ("attitude", "pitch", 1)],
            "CARGO": [("climate", "temp", 10), ("climate", "humidity", 10)],
        },
        "fft": {("vibration", "rms"), ("attitude", "roll")},
        "src": {"ENGINE": ["vibration", "temperature", "gas"],
                "BRIDGE": ["attitude"], "CARGO": ["climate", "hatch"]},
        "tolerant": False,   # 합성 데이터는 주기가 정확 — 윈도우 샘플 수 불일치 불허
    },
    "ship": {
        "sensors": {
            "ENGINE": [("vibration", "vib_rms_g", 1), ("vibration", "vib_peak_g", 1),
                       ("gas", "mq2_v", 2)],
            "BRIDGE": [("attitude", "roll", 1), ("attitude", "pitch", 1)],
            "CARGO": [("climate", "temp_c", 5), ("climate", "rh", 5)],
        },
        "fft": {("vibration", "vib_rms_g"), ("attitude", "roll")},
        "src": {"ENGINE": ["vibration", "gas"], "BRIDGE": ["attitude"],
                "CARGO": ["climate", "hatch"]},
        "tolerant": True,    # ESP32 주기 흔들림(~1.05s) — 윈도우를 격자 보간으로 채움
    },
}

SUBSYSTEM_SENSORS = dict(PROFILES["synthetic"]["sensors"])
SUBSYSTEMS = list(SUBSYSTEM_SENSORS)
FFT_SENSORS = set(PROFILES["synthetic"]["fft"])
SRC_CONTAINERS = dict(PROFILES["synthetic"]["src"])
TOLERANT_WINDOWS = False
ACTIVE_PROFILE = "synthetic"


def set_profile(name: str) -> None:
    """전역 센서 맵을 프로필로 교체 — 특징 계산/파이프라인 생성 전에 호출해야 한다."""
    global TOLERANT_WINDOWS, ACTIVE_PROFILE
    p = PROFILES[name]
    SUBSYSTEM_SENSORS.clear()
    SUBSYSTEM_SENSORS.update(p["sensors"])
    SUBSYSTEMS[:] = list(SUBSYSTEM_SENSORS)
    FFT_SENSORS.clear()
    FFT_SENSORS.update(p["fft"])
    SRC_CONTAINERS.clear()
    SRC_CONTAINERS.update(p["src"])
    TOLERANT_WINDOWS = bool(p["tolerant"])
    ACTIVE_PROFILE = name


META_COLS = ["window_start", "window_end", "label", "anomaly_type"]


def load_csv(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path, keep_default_na=False)
    df["label"] = df["label"].astype(int)
    df["t"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(None)  # naive UTC
    df["vals"] = df["values"].map(json.loads)
    return df


def feature_cols(fdf: pd.DataFrame) -> list[str]:
    return [c for c in fdf.columns if c not in META_COLS]


def _offsets(df_sub: pd.DataFrame, t0: pd.Timestamp) -> np.ndarray:
    return (df_sub["t"] - t0).dt.total_seconds().to_numpy()


def _window_matrix(off: np.ndarray, v: np.ndarray, starts: np.ndarray, step: int):
    """주기 step(s)의 정규 샘플을 (윈도우 × 윈도우 내 샘플) 행렬로 모은다.

    반환: (값 행렬 M, 윈도우 시작 기준 상대시각 행렬 trel)
    """
    i0 = np.ceil((starts - off[0]) / step - 1e-9).astype(int)
    count = int(np.ceil((starts[0] + WINDOW_S - off[0]) / step - 1e-9) - i0[0])
    idx = i0[:, None] + np.arange(count)[None, :]
    if idx.min() < 0 or idx.max() >= len(v):
        raise ValueError("윈도우가 데이터 범위를 벗어남 — CSV가 잘렸거나 샘플 주기가 다름")
    return v[idx], off[idx] - starts[:, None]


def _stats(M: np.ndarray, trel: np.ndarray, prefix: str, with_fft: bool) -> dict:
    out = {
        f"{prefix}.mean": M.mean(axis=1),
        f"{prefix}.std": M.std(axis=1),
        f"{prefix}.rms": np.sqrt((M * M).mean(axis=1)),
        f"{prefix}.max": M.max(axis=1),
    }
    tc = trel - trel.mean(axis=1, keepdims=True)
    out[f"{prefix}.slope"] = (tc * M).sum(axis=1) / np.maximum((tc * tc).sum(axis=1), 1e-9)
    if with_fft:
        d = M - M.mean(axis=1, keepdims=True)
        amp = np.abs(np.fft.rfft(d, axis=1))
        k = amp[:, 1:].argmax(axis=1) + 1          # DC 제외 최대 성분
        # 포물선 보간으로 빈 해상도(1/30 Hz)보다 정밀한 주파수 추정.
        # 보간 없이는 정상 roll 주기(9~11s)가 전부 같은 빈(0.1Hz)에 떨어져 상수가 되고,
        # 상수 특징은 IsolationForest가 분리에 쓰지 못한다.
        rows = np.arange(amp.shape[0])
        a = amp[rows, np.clip(k - 1, 0, amp.shape[1] - 1)]
        b = amp[rows, k]
        c = amp[rows, np.clip(k + 1, 0, amp.shape[1] - 1)]
        denom = a - 2.0 * b + c
        delta = np.where(np.abs(denom) > 1e-12, 0.5 * (a - c) / denom, 0.0)
        out[f"{prefix}.domfreq"] = (k + np.clip(delta, -0.5, 0.5)) / float(WINDOW_S)  # Hz
        # 주파수 변화가 여러 특징에서 동시에 드러나도록 진폭 불변 보조 특징 2개 추가.
        # (IsolationForest는 단일 특징만 벗어나는 이상에 둔감 — 특징 수에 희석된다)
        sm = (d[:, :-2] + d[:, 1:-1] + d[:, 2:]) / 3.0            # 3점 평활
        sgn = np.where(sm >= 0.0, 1, -1)
        zc = (sgn[:, 1:] != sgn[:, :-1]).sum(axis=1)
        out[f"{prefix}.zc_rate"] = zc / float(WINDOW_S)            # zero-crossing/초 ≈ 2/주기
        mad = np.abs(np.diff(sm, axis=1)).mean(axis=1)
        out[f"{prefix}.madr"] = mad / (sm.std(axis=1) + 1e-9)      # 평균|Δ|/std ∝ 각주파수
    return out


def _hatch_open_frac(df: pd.DataFrame, t0: pd.Timestamp, total_s: int, starts: np.ndarray) -> np.ndarray:
    """초 단위 hatch 상태 타임라인(이벤트 forward-fill, 초기 closed)에서 윈도우별 open 비율."""
    ind = np.zeros(total_s + WINDOW_S)
    h = df[(df["subsystem"] == "CARGO") & (df["container"] == "hatch")].sort_values("t")
    if len(h):
        offs = _offsets(h, t0).astype(int)
        states = h["vals"].map(lambda d: 1.0 if d["state"] == "open" else 0.0).to_numpy()
        cur, prev = 0.0, 0
        for o, st in zip(offs, states):
            o = max(int(o), 0)
            ind[prev:o] = cur
            cur, prev = st, o
        ind[prev:] = cur
    csum = np.concatenate([[0.0], np.cumsum(ind)])
    return (csum[starts + WINDOW_S] - csum[starts]) / float(WINDOW_S)


def _window_labels(df: pd.DataFrame, subsystem: str, t0: pd.Timestamp, starts: np.ndarray):
    n = starts.shape[0]
    label = np.zeros(n, dtype=int)
    atype = np.full(n, "", dtype=object)
    rows = df[(df["subsystem"] == subsystem) & (df["label"] == 1)]
    if len(rows):
        offs = _offsets(rows, t0)
        types = rows["anomaly_type"].to_numpy()
        for o, ty in zip(offs, types):
            k_lo = max(int(np.floor((o - WINDOW_S) / STRIDE_S)) + 1, 0)
            k_hi = min(int(np.floor(o / STRIDE_S)), n - 1)
            if k_hi >= k_lo:
                label[k_lo:k_hi + 1] = 1
                atype[k_lo:k_hi + 1] = ty

    # hatch는 이벤트 스트림이라 라벨 행(open)이 한 개뿐 — 개방 '구간'(open~다음 closed)과
    # 겹치는 윈도우 전체를 라벨해야 open_frac 특징과 평가가 맞는다.
    if subsystem == "CARGO":
        h = df[(df["subsystem"] == "CARGO") & (df["container"] == "hatch")].sort_values("t")
        if len(h):
            offs = _offsets(h, t0)
            labs = h["label"].to_numpy()
            types = h["anomaly_type"].to_numpy()
            split_end = float(starts[-1] + WINDOW_S)
            for i in range(len(h)):
                if labs[i] != 1:
                    continue
                a = offs[i]
                b = offs[i + 1] if i + 1 < len(h) else split_end
                k_lo = max(int(np.floor((a - WINDOW_S) / STRIDE_S)) + 1, 0)
                k_hi = min(int(np.ceil(b / STRIDE_S)) - 1, n - 1)
                if k_hi >= k_lo:
                    label[k_lo:k_hi + 1] = 1
                    atype[k_lo:k_hi + 1] = types[i]
    return label, atype


def subsystem_features(df: pd.DataFrame, subsystem: str) -> pd.DataFrame:
    """한 분할 전체를 윈도우 특징 DataFrame으로 (특징 + window_start/end, label, anomaly_type)."""
    t0 = df["t"].min()
    total_s = int(round((df["t"].max() - t0).total_seconds())) + 1
    starts = np.arange(0, total_s - WINDOW_S + 1, STRIDE_S, dtype=int)

    feats: dict[str, np.ndarray] = {}
    for cont, key, step in SUBSYSTEM_SENSORS[subsystem]:
        s = df[(df["subsystem"] == subsystem) & (df["container"] == cont)].sort_values("t")
        off = _offsets(s, t0)
        v = s["vals"].map(lambda d, k=key: float(d[k])).to_numpy()
        M, trel = _window_matrix(off, v, starts.astype(float), step)
        feats.update(_stats(M, trel, f"{cont}.{key}", (cont, key) in FFT_SENSORS))
    if subsystem == "CARGO":
        feats["hatch.open_frac"] = _hatch_open_frac(df, t0, total_s, starts)

    label, atype = _window_labels(df, subsystem, t0, starts)
    fdf = pd.DataFrame(feats)
    fdf.insert(0, "window_start", t0 + pd.to_timedelta(starts, unit="s"))
    fdf.insert(1, "window_end", t0 + pd.to_timedelta(starts + WINDOW_S, unit="s"))
    fdf["label"] = label
    fdf["anomaly_type"] = atype
    return fdf


if __name__ == "__main__":
    for split in ("train", "eval"):
        p = ROOT / "data" / split / f"{split}.csv"
        if not p.exists():
            print(f"{p} 없음 — 먼저 generator.py 실행")
            continue
        df = load_csv(p)
        for sub in SUBSYSTEMS:
            fdf = subsystem_features(df, sub)
            print(f"{split:<5} {sub:<6} windows={len(fdf):>6}  features={len(feature_cols(fdf)):>2}  "
                  f"labeled={int(fdf['label'].sum())}")
