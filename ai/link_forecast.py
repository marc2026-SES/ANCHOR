#!/usr/bin/env python3
"""항로 전방 통신(LTE 커버리지) 예측.

위치·침로·속력을 직선 투영해 "앞으로 horizon_min 안에 LTE 커버리지를
벗어나는가/복귀하는가"를 판단한다. 커버리지는 config.yaml `link_forecast.lte_cells`
의 합성 기지국 원(중심 좌표, 반경 km)으로 정의한다 — 합성 규칙은 README 참조.

이벤트 (분석 AE가 oneM2M linkForecast 컨테이너에 CIN으로 기록):
  mode_init       시작 시 현재 모드 보고 (lte | vsat_only)
  loss_predicted  LTE 이탈 예상이 warn_min 안으로 들어옴 (etaS)
  lte_lost        커버리지 이탈 → VSAT 단독(협대역) 모드 (regainEtaS: 전방에 복귀가 보이면)
  lte_restored    커버리지 복귀

공통 필드: eventId(결정적 — 재전송 시 중복 제거), routeMode(coastal=연안 LTE·edge 경유 /
opensea=공해 VSAT 직접) — oneM2M 파트 '경로 선택'의 링크 입력(현재 시뮬레이터) 대체용.

정책 연계: loss_predicted 때 밀린 summary를 미리 flush(batchNotify)하고,
VSAT 구간에서는 anomaly만 즉시 전송 — 전송 자체는 oneM2M 몫, 여기는 판단만.

단독 시뮬: python link_forecast.py   # data/eval/eval.csv 위치 재생
          → 이벤트 타임라인 출력 + results/link_events.jsonl
"""
from __future__ import annotations

import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def event_id(kind: str, iso_ts: str, suffix: str = "") -> str:
    """결정적 이벤트 ID — 같은 창·시각이면 재전송해도 같은 ID (누락 복구 대조·중복 제거용)."""
    return "-".join(p for p in (kind, suffix, iso_ts.replace("-", "").replace(":", "")) if p)


def _dist_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """등장방형 근사 거리(km) — 수십 km 해역 스케일에 충분."""
    kx = 111.32 * math.cos(math.radians((lat1 + lat2) / 2.0))
    return math.hypot((lat1 - lat2) * 111.32, (lon1 - lon2) * kx)


def _project(lat: float, lon: float, cog_deg: float, dist_km: float) -> tuple[float, float]:
    b = math.radians(cog_deg)
    dlat = dist_km * math.cos(b) / 111.32
    dlon = dist_km * math.sin(b) / max(111.32 * math.cos(math.radians(lat)), 1e-9)
    return lat + dlat, lon + dlon


class LinkForecaster:
    """위치 업데이트를 받아 커버리지 상태 전환·예측 이벤트를 on_event(dict)로 알린다."""

    def __init__(self, cfg: dict, on_event):
        self.cells = [(float(c["lat"]), float(c["lon"]), float(c["radius_km"]),
                       str(c.get("name", "?"))) for c in cfg.get("lte_cells", [])]
        self.horizon_s = int(cfg.get("horizon_min", 30)) * 60
        self.step_s = int(cfg.get("step_s", 30))
        self.interval_s = float(cfg.get("interval_s", 15))
        self.warn_s = int(cfg.get("warn_min", 10)) * 60
        self.on_event = on_event
        self.mode: str | None = None          # 'lte' | 'vsat_only'
        self.loss_warned = False
        self._last_ep = -1e18

    def _in_lte(self, lat: float, lon: float) -> bool:
        return any(_dist_km(lat, lon, clat, clon) <= r for clat, clon, r, _ in self.cells)

    def _first_change_s(self, lat: float, lon: float, cog: float,
                        v_kmh: float, in_cov: bool) -> int | None:
        """직선 투영으로 커버리지 상태가 처음 바뀌는 전방 시점(초)."""
        if v_kmh < 0.05:
            return None
        t = self.step_s
        while t <= self.horizon_s:
            plat, plon = _project(lat, lon, cog, v_kmh * t / 3600.0)
            if self._in_lte(plat, plon) != in_cov:
                return t
            t += self.step_s
        return None

    def update(self, lat: float, lon: float, sog_kn: float, cog_deg: float,
               ep: float, iso_ts: str) -> None:
        if ep - self._last_ep < self.interval_s:
            return
        self._last_ep = ep
        in_cov = self._in_lte(lat, lon)
        mode = "lte" if in_cov else "vsat_only"
        change_s = self._first_change_s(lat, lon, cog_deg, max(float(sog_kn), 0.0) * 1.852, in_cov)
        base = {"ts": iso_ts, "eventId": event_id("lnk", iso_ts),
                "mode": mode, "routeMode": "coastal" if in_cov else "opensea",
                "lat": round(lat, 5), "lon": round(lon, 5),
                "sogKn": round(float(sog_kn), 2), "cogDeg": round(float(cog_deg), 1)}

        if self.mode is None:                                  # 첫 위치: 현재 모드 보고
            self.mode = mode
            self.on_event({"type": "mode_init", **base})
        elif mode != self.mode:                                # 상태 전환
            self.mode = mode
            self.loss_warned = False
            ev = {"type": "lte_lost" if mode == "vsat_only" else "lte_restored", **base}
            if mode == "vsat_only" and change_s is not None:
                ev["regainEtaS"] = change_s
            self.on_event(ev)
        elif mode == "lte" and not self.loss_warned \
                and change_s is not None and change_s <= self.warn_s:
            self.loss_warned = True                            # 이탈 임박 경고 (1회)
            self.on_event({"type": "loss_predicted", "etaS": change_s, **base})
        elif mode == "lte" and self.loss_warned \
                and (change_s is None or change_s > 1.5 * self.warn_s):
            self.loss_warned = False                           # 항로 변경 등으로 해제


# ---------------------------------------------------------------- 단독 시뮬

def replay(csv_path: str | Path, cfg: dict) -> list[dict]:
    """eval.csv의 BRIDGE/position 행을 시간순으로 흘려 이벤트 타임라인 생성."""
    from datetime import datetime, timezone

    import pandas as pd

    df = pd.read_csv(csv_path, keep_default_na=False)
    df = df[(df["subsystem"] == "BRIDGE") & (df["container"] == "position")]
    events: list[dict] = []

    def on_event(ev: dict) -> None:
        events.append(ev)
        extra = f" etaS={ev['etaS']}" if "etaS" in ev else \
            (f" regainEtaS={ev['regainEtaS']}" if "regainEtaS" in ev else "")
        print(f"{ev['ts']} {ev['type']:<15} mode={ev['mode']}{extra}")

    fc = LinkForecaster(cfg, on_event)
    for r in df.itertuples(index=False):
        v = json.loads(r.values)
        ep = datetime.strptime(r.ts, TS_FMT).replace(tzinfo=timezone.utc).timestamp()
        fc.update(v["lat"], v["lon"], v.get("sog", 0.0), v.get("cog", 0.0), ep, r.ts)
    return events


if __name__ == "__main__":
    import argparse

    import yaml

    ap = argparse.ArgumentParser(description="링크 예측 단독 시뮬 (상세: README.md)")
    ap.add_argument("--csv", default=str(ROOT / "data" / "eval" / "eval.csv"))
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    args = ap.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))["link_forecast"]
    events = replay(args.csv, cfg)
    out = ROOT / "results" / "link_events.jsonl"
    out.parent.mkdir(exist_ok=True)
    out.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
                   encoding="utf-8")
    print(f"{len(events)} events -> {out.relative_to(ROOT)}")
