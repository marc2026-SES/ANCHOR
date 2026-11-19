#!/usr/bin/env python3
"""항로 이탈 탐지 — 배마다 과거 항적으로 '평소 다니는 길'을 학습한다 (연안 fleet-ai용).

학습: 정상 항적(위치·침로)만 기억한다. 판정: 지금 위치·침로에서 가장 가까운 과거 지점까지의
거리(km)가 멀면 이탈. 침로 차이도 거리로 환산해 더하므로(반대 방향 = 2×HEAD_KM) 같은 길을
반대로 가거나, 꺾을 곳에서 직진하는 경우도 잡는다.

Isolation Forest가 아니라 최근접 이웃(NearestNeighbors)을 쓴다 — IF는 축에 나란히만 자르기
때문에 대각선으로 뻗은 가느다란 항로를 감싸지 못해, 같은 가상 항적 시험에서 이탈을 5~30km
뒤에야 잡았다(최근접 이웃은 1~2km). 상세 수치는 README 참고.

사용:
  python fleet_route_model.py status --tracks results/tracks.jsonl       # 배별 수집 현황
  python fleet_route_model.py train  --tracks results/tracks.jsonl --out models-fleet
  python fleet_route_model.py selftest
"""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import joblib
import numpy as np
from sklearn.neighbors import NearestNeighbors

HEAD_KM = 5.0            # 침로 차이의 거리 환산: (sin, cos) 차이 1 = 5km → 정반대 침로 = 10km
WARN_KM = 1.0            # 과거 항적에서 이만큼 떨어지면 warn (학습된 흩어짐이 더 크면 그 3배)
DANGER_KM = 3.0          # danger (학습된 흩어짐이 더 크면 그 8배)
LAP_AWAY_KM = 5.0        # 한 바퀴 판정: 출발 상태에서 이만큼 멀어졌다가
LAP_BACK_KM = 1.0        #               이 안으로 돌아오면 한 바퀴
MODEL_FILE = "routes.joblib"


def _features(samples: np.ndarray, ref: tuple[float, float]) -> np.ndarray:
    """(lat, lon, cog°) → 기준점 대비 (x_km, y_km, HEAD_KM·sin, HEAD_KM·cos)."""
    lat, lon, cog = samples[:, 0], samples[:, 1], np.radians(samples[:, 2])
    kx = 111.32 * math.cos(math.radians(ref[0]))
    return np.column_stack([(lon - ref[1]) * kx, (lat - ref[0]) * 110.57,
                            HEAD_KM * np.sin(cog), HEAD_KM * np.cos(cog)])


def lap_complete(X: np.ndarray) -> bool:
    """항적이 한 바퀴를 덮었는가 — 첫 상태(위치·침로)에서 멀어졌다가 다시 돌아왔는지로 판정."""
    d = np.linalg.norm(X - X[0], axis=1)
    away = np.argmax(d > LAP_AWAY_KM)
    return bool(d[away] > LAP_AWAY_KM and (d[away:] < LAP_BACK_KM).any())


def train_ship(samples: np.ndarray) -> dict | None:
    """한 배의 정상 항적으로 모델을 만든다. 한 바퀴를 못 채웠으면 None (안 다닌 구간이 이탈로 보인다)."""
    ref = (float(samples[:, 0].mean()), float(samples[:, 1].mean()))
    X = _features(samples, ref)
    if len(X) < 50 or not lap_complete(X):
        return None
    hold = np.arange(len(X)) % 5 == 0              # 길 전체에 고르게 퍼진 검증 표본
    nn = NearestNeighbors(n_neighbors=1).fit(X[~hold])
    spread = float(np.quantile(nn.kneighbors(X[hold])[0][:, 0], 0.995))
    return {"ref": ref, "nn": NearestNeighbors(n_neighbors=1).fit(X), "n": int(len(X)),
            "spread_km": round(spread, 3),
            "warn_km": round(max(WARN_KM, 3.0 * spread), 3),
            "danger_km": round(max(DANGER_KM, 8.0 * spread), 3)}


def load_tracks(path: str | Path, since_min: float | None = None) -> dict[str, np.ndarray]:
    """tracks.jsonl → 배별 (lat, lon, cog) 배열 (시간순). since_min이 있으면 최근 그 분만."""
    rows: dict[str, list] = {}
    t_min = time.time() - since_min * 60.0 if since_min else 0.0
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r["t"] >= t_min:
                rows.setdefault(r["id"], []).append((r["t"], r["lat"], r["lon"], r["cog"]))
    return {sid: np.array(sorted(v))[:, 1:] for sid, v in rows.items()}


def train(tracks_path: str | Path, out_dir: str | Path, since_min: float | None = None) -> dict:
    tracks = load_tracks(tracks_path, since_min)
    models, card = {}, {}
    for sid, samples in sorted(tracks.items()):
        m = train_ship(samples)
        if m is None:
            print(f"{sid:<18} 표본 {len(samples):>5} — 한 바퀴 미완, 학습 보류")
            continue
        models[sid] = m
        card[sid] = {k: m[k] for k in ("n", "spread_km", "warn_km", "danger_km")}
        print(f"{sid:<18} 표본 {m['n']:>5}  흩어짐 {m['spread_km']:.2f}km  "
              f"warn {m['warn_km']:.1f}km  danger {m['danger_km']:.1f}km")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    joblib.dump({"trained_at": time.time(), "version": stamp, "ships": models}, out / MODEL_FILE)
    (out / "routes_card.json").write_text(json.dumps(
        {"version": stamp, "head_km": HEAD_KM, "ships": card}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"-> {out / MODEL_FILE} ({len(models)}척, 버전 {stamp})")
    return card


class RouteModels:
    """스캐너가 쓰는 판정기. 모델 파일이 바뀌면(재학습) 다시 읽는다."""

    def __init__(self, models_dir: str | Path):
        self.path = Path(models_dir) / MODEL_FILE
        self.mtime = 0.0
        self.trained_at = 0.0
        self.version = ""
        self.ships: dict[str, dict] = {}
        self.reload()

    def reload(self) -> bool:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return False
        if mtime == self.mtime:
            return False
        blob = joblib.load(self.path)
        self.mtime, self.trained_at = mtime, float(blob["trained_at"])
        self.version, self.ships = blob["version"], blob["ships"]
        return True

    def check(self, ship_id: str, lat: float, lon: float, cog: float) -> tuple[float, str] | None:
        """(과거 항적까지 거리 km, 'normal'|'warn'|'danger'). 학습 안 된 배는 None."""
        m = self.ships.get(ship_id)
        if m is None:
            return None
        d = float(m["nn"].kneighbors(_features(np.array([[lat, lon, cog]]), m["ref"]))[0][0, 0])
        return d, ("danger" if d >= m["danger_km"] else "warn" if d >= m["warn_km"] else "normal")


# ---------------------------------------------------------------- 자체 테스트

def _simulate(path_km: list[tuple[float, float]], step_km: float, laps: float) -> np.ndarray:
    """꺾은선을 왕복하는 가상 항적 (lat, lon, cog). 원점 (34.4N, 129.5E) 기준 km 좌표."""
    seq = path_km + path_km[-2::-1]
    seg = list(zip(seq[:-1], seq[1:]))
    lens = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in seg]
    kx = 111.32 * math.cos(math.radians(34.4))
    out, s = [], 0.0
    while s < sum(lens) * laps:
        r = s % sum(lens)
        for (a, b), ln in zip(seg, lens):
            if r <= ln:
                x, y = a[0] + (b[0] - a[0]) * r / ln, a[1] + (b[1] - a[1]) * r / ln
                cog = math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) % 360
                out.append((34.4 + y / 110.57, 129.5 + x / kx, cog))
                break
            r -= ln
        s += step_km
    return np.array(out)


def run_selftest() -> None:
    route = [(0.0, 0.0), (40.0, 30.0), (90.0, 20.0), (120.0, -40.0)]   # 대각선 구간이 있는 왕복 항로
    track = _simulate(route, 0.5, 2.0)
    lap = len(track) // 2
    assert train_ship(track[: lap // 3]) is None, "한 바퀴 미완인데 학습됨"
    lap += 10                       # 출발 상태(위치·침로)로 되돌아오는 건 다음 바퀴 첫 표본부터
    m = train_ship(track[:lap])
    assert m is not None, "한 바퀴를 덮었는데 학습 보류"
    rm = RouteModels.__new__(RouteModels)
    rm.ships = {"t": m}
    lv = [rm.check("t", *row)[1] for row in track[lap:]]
    assert lv.count("normal") == len(lv), f"정상 항적에서 경고 {len(lv) - lv.count('normal')}건"
    # 둘째 경유점(40, 30)에서 꺾지 않고 직진 — 침로 53.1°로 계속 감
    kx = 111.32 * math.cos(math.radians(34.4))
    cog = math.degrees(math.atan2(40.0, 30.0))
    first = {}
    for i in range(1, 60):
        x, y = 40.0 + 0.5 * i * math.sin(math.radians(cog)), 30.0 + 0.5 * i * math.cos(math.radians(cog))
        d, level = rm.check("t", 34.4 + y / 110.57, 129.5 + x / kx, cog)
        first.setdefault(level, 0.5 * i)
    assert first.get("warn", 99) <= 2.0 and first.get("danger", 99) <= 5.0, f"이탈 탐지가 늦음 {first}"
    print(f"셀프테스트 통과: 한 바퀴 미완 → 보류, 정상 왕복 {len(lv)}표본 경고 0건, "
          f"직진 이탈 warn {first['warn']:.1f}km · danger {first['danger']:.1f}km 지점 "
          f"(흩어짐 {m['spread_km']}km)")


def main() -> None:
    ap = argparse.ArgumentParser(description="항로 이탈 탐지 모델 (상세: 파일 docstring)")
    sp = ap.add_subparsers(dest="cmd", required=True)
    for name in ("status", "train"):
        p = sp.add_parser(name)
        p.add_argument("--tracks", default="results/tracks.jsonl")
        p.add_argument("--since-min", type=float, default=None, help="최근 N분의 항적만 사용")
        if name == "train":
            p.add_argument("--out", default="models-fleet")
    sp.add_parser("selftest")
    args = ap.parse_args()
    if args.cmd == "selftest":
        run_selftest()
    elif args.cmd == "train":
        train(args.tracks, args.out, args.since_min)
    else:
        for sid, s in sorted(load_tracks(args.tracks, args.since_min).items()):
            done = len(s) >= 50 and lap_complete(_features(s, (float(s[:, 0].mean()), float(s[:, 1].mean()))))
            print(f"{sid:<18} 표본 {len(s):>5}  한 바퀴 {'완료' if done else '미완'}")


if __name__ == "__main__":
    main()
