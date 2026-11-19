#!/usr/bin/env python3
"""ANCHOR AI 합성 데이터 생성기.

선박 4개 노드(BRIDGE/ENGINE/CARGO/ENV)의 정상 운항 데이터(train)와,
라벨이 달린 6종 이상(spike, drift, level_shift, freq_change, cargo_temp,
hatch_open)을 주입한 평가 데이터(eval)를 만든다.
생성 규칙 상세는 README.md에 문서화되어 있다 (CLAUDE.md 규칙).

출력
  data/train/train.csv    정상만 (label 전부 0)
  data/eval/eval.csv      이상 포함 (label, anomaly_type)
  data/eval/events.json   주입한 이상 이벤트 원장 (탐지 지연 평가용)

CSV 컬럼: ts, ship_id, subsystem, container, values(JSON), label, anomaly_type

사용 예
  python generator.py --hours 12 --seed 42 --anomaly-rate 1.0
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent

SHIP_ID = "SHIP-01"
TRAIN_START = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

# 이상 유형: (subsystem, container, (최소 지속 s, 최대 지속 s))
ANOMALY_SPECS = {
    "spike": ("ENGINE", "gas", (20, 90)),
    "drift": ("ENGINE", "temperature", (600, 1800)),
    "level_shift": ("ENGINE", "vibration", (300, 1200)),
    "freq_change": ("BRIDGE", "attitude", (300, 1200)),
    "cargo_temp": ("CARGO", "climate", (600, 1800)),
    "hatch_open": ("CARGO", "hatch", (180, 600)),
}
ANOMALY_TYPES = list(ANOMALY_SPECS)


# ---------------------------------------------------------------- 공통 유틸

def ts_strings(start_dt: datetime, offsets: np.ndarray):
    """시작 시각 + 초 오프셋 배열 → ISO8601(UTC, Z) 문자열."""
    idx = pd.Timestamp(start_dt) + pd.to_timedelta(offsets, unit="s")
    return idx.strftime(TS_FMT)


def ou(rng: np.random.Generator, n: int, sigma: float, tau: float, dt: float = 1.0) -> np.ndarray:
    """평균 0으로 회귀하는 랜덤워크(Ornstein-Uhlenbeck). 정상상태 표준편차 sigma."""
    a = math.exp(-dt / tau)
    s = sigma * math.sqrt(1.0 - a * a)
    x = np.empty(n)
    x[0] = rng.normal(0.0, sigma)
    eps = rng.normal(0.0, 1.0, size=n)
    for i in range(1, n):
        x[i] = a * x[i - 1] + s * eps[i]
    return x


def edge_envelope(tau: np.ndarray, dur: float, rise: float, fall: float) -> np.ndarray:
    """윈도우 내 경과초 tau에 대한 사다리꼴 envelope: 0→1(rise초) … 1→0(fall초)."""
    up = np.clip(tau / max(rise, 1e-9), 0.0, 1.0)
    down = np.clip((dur - tau) / max(fall, 1e-9), 0.0, 1.0)
    return np.minimum(up, down)


def ramp_hold_decay(tau: np.ndarray, dur: float, rise_frac: float = 0.6, hold_frac: float = 0.2) -> np.ndarray:
    """0→1 선형 램프(rise_frac), 유지(hold_frac), 윈도우 끝까지 1→0 감쇠."""
    rise = max(dur * rise_frac, 1e-9)
    decay_start = dur * (rise_frac + hold_frac)
    decay = max(dur - decay_start, 1e-9)
    up = np.clip(tau / rise, 0.0, 1.0)
    down = np.where(tau <= decay_start, 1.0, np.clip(1.0 - (tau - decay_start) / decay, 0.0, 1.0))
    return up * down


def spike_profile(tau: np.ndarray, dur: float) -> np.ndarray:
    """급등 후 지수 감쇠, 윈도우 끝에서 0."""
    rise = max(0.15 * dur, 1e-9)
    decay_tau = max(0.22 * dur, 1e-9)
    r = np.clip(tau / rise, 0.0, 1.0) * np.exp(-np.clip(tau - rise, 0.0, None) / decay_tau)
    r *= np.clip((dur - tau) / max(0.1 * dur, 1e-9), 0.0, 1.0)
    return r


def events_for(events: list[dict], sub: str, cont: str) -> list[dict]:
    return [ev for ev in events if ev["subsystem"] == sub and ev["container"] == cont]


def labels_for(t: np.ndarray, events: list[dict], sub: str, cont: str):
    """스트림 시각 배열 t(초 오프셋)에 대해 label/anomaly_type 배열 생성 (윈도우 [start, end))."""
    label = np.zeros(t.shape[0], dtype=int)
    atype = np.full(t.shape[0], "", dtype=object)
    for ev in events_for(events, sub, cont):
        m = (t >= ev["start_s"]) & (t < ev["end_s"])
        label[m] = 1
        atype[m] = ev["type"]
    return label, atype


def pack(start_dt: datetime, sub: str, cont: str, offsets: np.ndarray,
         values_dicts: list[dict], label: np.ndarray, atype: np.ndarray) -> pd.DataFrame:
    return pd.DataFrame({
        "ts": ts_strings(start_dt, offsets),
        "ship_id": SHIP_ID,
        "subsystem": sub,
        "container": cont,
        "values": [json.dumps(v, ensure_ascii=False, separators=(",", ":")) for v in values_dicts],
        "label": label,
        "anomaly_type": atype,
    })


# ---------------------------------------------------------------- 신호 모델

def engine_load(rng: np.random.Generator, n: int, departure: bool) -> np.ndarray:
    """엔진 부하 load(t)∈[0.05, 0.95]. departure=True면 정박(10분)→램프업(20분)→순항."""
    t = np.arange(n, dtype=float)
    phase = rng.uniform(0.0, 2.0 * math.pi)
    cruise = 0.70 + 0.05 * np.sin(2.0 * math.pi * t / 7200.0 + phase) + ou(rng, n, 0.03, 600.0)
    if departure:
        port_s, ramp_s = 600.0, 1200.0
        frac = np.clip((t - port_s) / ramp_s, 0.0, 1.0)
        load = 0.08 + (cruise - 0.08) * frac
    else:
        load = cruise
    return np.clip(load, 0.05, 0.95)


def generate_split(rng: np.random.Generator, hours: float, start_dt: datetime,
                   departure: bool, events: list[dict]) -> pd.DataFrame:
    """한 분할(train 또는 eval)의 전체 스트림 생성. events가 비면 정상만."""
    n1 = int(round(hours * 3600))
    t1 = np.arange(n1, dtype=float)
    load = engine_load(rng, n1, departure)
    frames: list[pd.DataFrame] = []

    # --- BRIDGE/position (1s): 항적. sog는 부하에 비례, 정박 중 ~0.3kn
    sog = np.clip(14.0 * (load - 0.06), 0.0, None) + rng.normal(0.0, 0.12, n1)
    sog = np.clip(sog, 0.0, None)
    # 항로는 MEC 파트 시나리오(anchor-korea-strait)와 같은 해역: 부산→쓰시마 동쪽→하카타 (방위 ~143°).
    # train은 부산항 출항, eval은 쓰시마 북동(tsushima-north 셀 내부, 이탈 ~15분 전)에서 시작
    cog = 143.0 + ou(rng, n1, 2.5, 3600.0)
    lat0, lon0 = (35.08, 129.05) if departure else (34.52, 129.675)
    v_ms = sog * 0.514444
    hdg = np.deg2rad(cog)
    dlat = v_ms * np.cos(hdg) / 111_320.0
    dlon = v_ms * np.sin(hdg) / (111_320.0 * math.cos(math.radians(lat0)))
    lat = lat0 + np.cumsum(dlat)
    lon = lon0 + np.cumsum(dlon)
    vals = [{"lat": float(round(a, 6)), "lon": float(round(b, 6)),
             "sog": float(round(s, 2)), "cog": float(round(c, 1))}
            for a, b, s, c in zip(lat, lon, sog, cog)]
    lab, aty = labels_for(t1, events, "BRIDGE", "position")
    frames.append(pack(start_dt, "BRIDGE", "position", t1, vals, lab, aty))

    # --- BRIDGE/attitude (1s): roll = 주기 8~12s 사인파(시변 주기·진폭) + 노이즈
    period = rng.uniform(9.0, 11.0) \
        + 0.5 * np.sin(2.0 * math.pi * t1 / 1800.0 + rng.uniform(0, 2 * math.pi)) \
        + ou(rng, n1, 0.15, 120.0)
    period = np.clip(period, 8.5, 11.5)
    for ev in events_for(events, "BRIDGE", "attitude"):   # freq_change: 주기를 대역 밖으로
        m = (t1 >= ev["start_s"]) & (t1 < ev["end_s"])
        tau = t1[m] - ev["start_s"]
        w = edge_envelope(tau, ev["end_s"] - ev["start_s"], 30.0, 30.0)
        period[m] = period[m] * (1.0 - w) + ev["params"]["target_period_s"] * w
    amp = np.clip(3.0 + 0.8 * np.sin(2.0 * math.pi * t1 / 3600.0 + rng.uniform(0, 2 * math.pi))
                  + ou(rng, n1, 0.2, 300.0), 1.2, 4.8)
    phi = 2.0 * math.pi * np.cumsum(1.0 / period) + rng.uniform(0, 2 * math.pi)
    roll = amp * np.sin(phi) + rng.normal(0.0, 0.25, n1)
    p_period = 6.5 + 0.3 * np.sin(2.0 * math.pi * t1 / 2400.0 + rng.uniform(0, 2 * math.pi))
    p_amp = np.clip(1.0 + 0.25 * np.sin(2.0 * math.pi * t1 / 3000.0 + rng.uniform(0, 2 * math.pi))
                    + ou(rng, n1, 0.08, 300.0), 0.3, 2.0)
    pitch = p_amp * np.sin(2.0 * math.pi * np.cumsum(1.0 / p_period) + rng.uniform(0, 2 * math.pi)) \
        + rng.normal(0.0, 0.15, n1)
    vals = [{"roll": float(round(r, 2)), "pitch": float(round(p, 2))} for r, p in zip(roll, pitch)]
    lab, aty = labels_for(t1, events, "BRIDGE", "attitude")
    frames.append(pack(start_dt, "BRIDGE", "attitude", t1, vals, lab, aty))

    # --- ENGINE/vibration (1s): rms = 0.4 + 1.8*load + 노이즈
    rms = 0.4 + 1.8 * load + ou(rng, n1, 0.05, 120.0) + rng.normal(0.0, 0.06, n1)
    for ev in events_for(events, "ENGINE", "vibration"):  # level_shift: 계단 상승
        m = (t1 >= ev["start_s"]) & (t1 < ev["end_s"])
        tau = t1[m] - ev["start_s"]
        env = edge_envelope(tau, ev["end_s"] - ev["start_s"], 5.0, 15.0)
        rms[m] *= 1.0 + (ev["params"]["factor"] - 1.0) * env
    rms = np.clip(rms, 0.05, None)
    vals = [{"rms": float(round(x, 3))} for x in rms]
    lab, aty = labels_for(t1, events, "ENGINE", "vibration")
    frames.append(pack(start_dt, "ENGINE", "vibration", t1, vals, lab, aty))

    # --- ENGINE/temperature (5s): 82 + 6*load(5분 평활) + 작은 평균회귀 랜덤워크
    t5 = np.arange(0.0, n1, 5.0)
    n5 = t5.shape[0]
    load_sm = pd.Series(load[::5]).rolling(60, min_periods=1).mean().to_numpy()
    temp = 82.0 + 6.0 * load_sm + ou(rng, n5, 0.6, 1800.0, dt=5.0) + rng.normal(0.0, 0.05, n5)
    for ev in events_for(events, "ENGINE", "temperature"):  # drift: 점진 상승→유지→회복
        m = (t5 >= ev["start_s"]) & (t5 < ev["end_s"])
        tau = t5[m] - ev["start_s"]
        temp[m] += ev["params"]["delta_c"] * ramp_hold_decay(tau, ev["end_s"] - ev["start_s"])
    vals = [{"temp": float(round(x, 2))} for x in temp]
    lab, aty = labels_for(t5, events, "ENGINE", "temperature")
    frames.append(pack(start_dt, "ENGINE", "temperature", t5, vals, lab, aty))

    # --- ENGINE/gas (5s): 낮은 baseline ~5ppm
    ppm = 5.0 + ou(rng, n5, 0.7, 300.0, dt=5.0) + rng.normal(0.0, 0.25, n5)
    for ev in events_for(events, "ENGINE", "gas"):        # spike: 급등 후 감쇠
        m = (t5 >= ev["start_s"]) & (t5 < ev["end_s"])
        tau = t5[m] - ev["start_s"]
        ppm[m] += ev["params"]["delta_ppm"] * spike_profile(tau, ev["end_s"] - ev["start_s"])
    ppm = np.clip(ppm, 0.3, None)
    vals = [{"ppm": float(round(x, 2))} for x in ppm]
    lab, aty = labels_for(t5, events, "ENGINE", "gas")
    frames.append(pack(start_dt, "ENGINE", "gas", t5, vals, lab, aty))

    # --- CARGO/climate (10s): 리퍼 화물칸 temp ~4°C, humidity ~85%
    t10 = np.arange(0.0, n1, 10.0)
    n10 = t10.shape[0]
    ctemp = 4.0 + ou(rng, n10, 0.3, 1200.0, dt=10.0) + rng.normal(0.0, 0.05, n10)
    for ev in events_for(events, "CARGO", "climate"):     # cargo_temp: 램프 상승
        m = (t10 >= ev["start_s"]) & (t10 < ev["end_s"])
        tau = t10[m] - ev["start_s"]
        ctemp[m] += ev["params"]["delta_c"] * ramp_hold_decay(tau, ev["end_s"] - ev["start_s"])
    hum = np.clip(85.0 + ou(rng, n10, 1.5, 900.0, dt=10.0) + rng.normal(0.0, 0.3, n10), 55.0, 98.0)
    vals = [{"temp": float(round(a, 2)), "humidity": float(round(h, 1))} for a, h in zip(ctemp, hum)]
    lab, aty = labels_for(t10, events, "CARGO", "climate")
    frames.append(pack(start_dt, "CARGO", "climate", t10, vals, lab, aty))

    # --- CARGO/hatch (이벤트): 상태 변화 시에만 행. 정박 중 open은 정상.
    hatch_rows: list[tuple[float, str, int, str]] = []
    if departure:  # 출항 전 정박 중 하역 해치 개방(정상)
        o = 120.0 + float(rng.integers(0, 60))
        c = 480.0 + float(rng.integers(0, 120))
        hatch_rows += [(o, "open", 0, ""), (c, "closed", 0, "")]
    for ev in events_for(events, "CARGO", "hatch"):       # hatch_open: 항해 중 개방
        hatch_rows.append((float(ev["start_s"]), "open", 1, ev["type"]))
        hatch_rows.append((float(ev["end_s"]), "closed", 0, ""))
    if hatch_rows:
        hatch_rows.sort(key=lambda r: r[0])
        offs = np.array([r[0] for r in hatch_rows])
        vals = [{"state": r[1]} for r in hatch_rows]
        lab = np.array([r[2] for r in hatch_rows], dtype=int)
        aty = np.array([r[3] for r in hatch_rows], dtype=object)
        frames.append(pack(start_dt, "CARGO", "hatch", offs, vals, lab, aty))

    # --- ENV/barometric (30s): 1013hPa 부근 느린 변화
    t30 = np.arange(0.0, n1, 30.0)
    n30 = t30.shape[0]
    hpa = 1013.0 + 4.0 * np.sin(2.0 * math.pi * t30 / 43200.0 + rng.uniform(0, 2 * math.pi)) \
        + ou(rng, n30, 0.6, 1800.0, dt=30.0)
    vals = [{"hpa": float(round(x, 1))} for x in hpa]
    lab, aty = labels_for(t30, events, "ENV", "barometric")
    frames.append(pack(start_dt, "ENV", "barometric", t30, vals, lab, aty))

    # --- ENV/illuminance (30s): 주/야 사이클 + 구름 변동
    hod = (start_dt.hour + start_dt.minute / 60.0 + t30 / 3600.0) % 24.0
    elev = np.where((hod >= 6.0) & (hod <= 18.0), np.sin(math.pi * (hod - 6.0) / 12.0), 0.0)
    cloud = np.clip(0.85 + ou(rng, n30, 0.1, 1800.0, dt=30.0), 0.5, 1.0)
    lux = 45000.0 * np.clip(elev, 0.0, None) ** 1.3 * cloud + 5.0 + np.abs(rng.normal(0.0, 2.0, n30))
    vals = [{"lux": float(round(x, 1))} for x in lux]
    lab, aty = labels_for(t30, events, "ENV", "illuminance")
    frames.append(pack(start_dt, "ENV", "illuminance", t30, vals, lab, aty))

    df = pd.concat(frames, ignore_index=True)
    return df.sort_values(["ts", "subsystem", "container"], kind="stable", ignore_index=True)


# ---------------------------------------------------------------- 이상 스케줄링

def draw_params(rng: np.random.Generator, typ: str) -> dict:
    if typ == "spike":
        return {"delta_ppm": float(round(rng.uniform(60.0, 250.0), 1))}
    if typ == "drift":
        return {"delta_c": float(round(rng.uniform(10.0, 20.0), 1))}
    if typ == "level_shift":
        return {"factor": float(round(rng.uniform(1.5, 2.2), 2))}
    if typ == "freq_change":
        fast = bool(rng.random() < 0.5)
        target = rng.uniform(4.5, 6.5) if fast else rng.uniform(15.0, 19.0)
        return {"target_period_s": float(round(target, 1))}
    if typ == "cargo_temp":
        return {"delta_c": float(round(rng.uniform(8.0, 16.0), 1))}
    return {}  # hatch_open


def schedule_events(rng: np.random.Generator, hours: float, anomaly_rate: float) -> list[dict]:
    """eval 구간의 이상 이벤트 스케줄. 유형별 최소 1회, 같은 스트림 내 비중첩(간격 120s)."""
    if anomaly_rate <= 0.0:
        return []
    total = hours * 3600.0
    margin = min(600.0, 0.1 * total)
    n_events = max(len(ANOMALY_TYPES), int(round(hours * anomaly_rate)))
    order = ANOMALY_TYPES.copy()
    rng.shuffle(order)
    if n_events > len(order):
        order += list(rng.choice(ANOMALY_TYPES, size=n_events - len(order)))

    occupied: dict[tuple[str, str], list[tuple[float, float]]] = {}
    events: list[dict] = []
    for typ in order:
        sub, cont, (dmin, dmax) = ANOMALY_SPECS[typ]
        dur = float(rng.uniform(dmin, dmax))
        dur = min(dur, max(30.0, (total - 2.0 * margin) * 0.5))
        placed = False
        for _ in range(300):
            start = float(rng.uniform(margin, total - margin - dur))
            spans = occupied.setdefault((sub, cont), [])
            if all(start + dur + 120.0 < s or start - 120.0 > e for s, e in spans):
                spans.append((start, start + dur))
                placed = True
                break
        if not placed:
            print(f"[warn] {typ} 이벤트 배치 실패(공간 부족) — 건너뜀")
            continue
        start = float(round(start))
        end = float(round(start + dur))
        events.append({
            "type": typ, "subsystem": sub, "container": cont,
            "start_s": start, "end_s": end, "params": draw_params(rng, typ),
        })
    events.sort(key=lambda e: e["start_s"])
    return events


# ---------------------------------------------------------------- oneM2M 연계

def to_cin_payload(row) -> dict:
    """CSV 한 행(dict 또는 pandas Series) → oneM2M CIN(contentInstance) 생성 요청 바디.

    대상 리소스 경로 규약: <CSE>/<ship_id>/<subsystem>/<container>
    예: SHIP-01/ENGINE/gas 컨테이너 아래에 이 바디로 CIN을 생성한다.
    HTTP 호출과 헤더(X-M2M-Origin 등)는 onem2m_client.py 전담(CLAUDE.md 규칙) —
    이 함수는 바디만 만든다.
    """
    values = row["values"]
    if isinstance(values, str):
        values = json.loads(values)
    con = {"ts": row["ts"], **values}
    return {
        "m2m:cin": {
            "con": json.dumps(con, ensure_ascii=False, separators=(",", ":")),
            "lbl": [str(row["ship_id"]), str(row["subsystem"]), str(row["container"])],
        }
    }


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description="ANCHOR AI 합성 데이터 생성기 (상세: README.md)")
    ap.add_argument("--hours", type=float, default=12.0, help="분할당 길이(시간), train/eval 각각 (기본 12)")
    ap.add_argument("--seed", type=int, default=42, help="난수 seed (기본 42)")
    ap.add_argument("--anomaly-rate", type=float, default=1.0,
                    help="eval 이상 이벤트 비율(건/시간, 기본 1.0). 유형별 최소 1회 보장, 0이면 주입 안 함")
    args = ap.parse_args()
    if args.hours < 0.5:
        ap.error("--hours 는 0.5 이상이어야 합니다")

    ss = np.random.SeedSequence(args.seed)
    rng_train, rng_sched, rng_eval = (np.random.default_rng(c) for c in ss.spawn(3))

    train_dir = ROOT / "data" / "train"
    eval_dir = ROOT / "data" / "eval"
    train_dir.mkdir(parents=True, exist_ok=True)
    eval_dir.mkdir(parents=True, exist_ok=True)

    # train: 출항 포함 정상 운항만
    train_df = generate_split(rng_train, args.hours, TRAIN_START, departure=True, events=[])
    assert int(train_df["label"].sum()) == 0, "train에는 이상이 없어야 함"
    train_csv = train_dir / "train.csv"
    train_df.to_csv(train_csv, index=False)

    # eval: train 종료 직후부터 순항 + 이상 주입
    eval_start = TRAIN_START + timedelta(hours=args.hours)
    events = schedule_events(rng_sched, args.hours, args.anomaly_rate)
    eval_df = generate_split(rng_eval, args.hours, eval_start, departure=False, events=events)
    eval_csv = eval_dir / "eval.csv"
    eval_df.to_csv(eval_csv, index=False)

    events_out = {
        "meta": {
            "ship_id": SHIP_ID, "seed": args.seed, "hours": args.hours,
            "anomaly_rate": args.anomaly_rate,
            "eval_start": eval_start.strftime(TS_FMT),
            "eval_end": (eval_start + timedelta(hours=args.hours)).strftime(TS_FMT),
        },
        "events": [{
            "type": ev["type"], "subsystem": ev["subsystem"], "container": ev["container"],
            "start": (eval_start + timedelta(seconds=ev["start_s"])).strftime(TS_FMT),
            "end": (eval_start + timedelta(seconds=ev["end_s"])).strftime(TS_FMT),
            "duration_s": ev["end_s"] - ev["start_s"],
            "params": ev["params"],
        } for ev in events],
    }
    events_json = eval_dir / "events.json"
    events_json.write_text(json.dumps(events_out, ensure_ascii=False, indent=2), encoding="utf-8")

    # 요약 출력
    by_type = {t: sum(1 for e in events if e["type"] == t) for t in ANOMALY_TYPES}
    print(f"train: {len(train_df):>7,} rows -> {train_csv.relative_to(ROOT)}")
    print(f"eval : {len(eval_df):>7,} rows ({int(eval_df['label'].sum()):,} labeled) -> {eval_csv.relative_to(ROOT)}")
    print(f"events: {len(events)} {by_type} -> {events_json.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
