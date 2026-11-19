#!/usr/bin/env python3
"""CPA/TCPA 기반 경로 겹침(충돌 위험) 경고 — anchor-fleet(연안 MEC 앱) 이식용.

anchor-fleet이 MEC013 Location API로 받은 주변 선박 위치/속력/침로로
자선-타선 쌍마다 CPA(최근접 거리)와 TCPA(그 시점까지 남은 시간)를 계산해
위험 수준을 판정한다. oneM2M/CSE 의존이 없는 순수 계산 모듈이라 선박 게이트웨이,
MEC 앱 어디서든 import해 쓸 수 있다.

판정 기본값:
  warn    cpa < 0.5 nm 이고 0 < tcpa < 20 min
  danger  cpa < 0.25 nm 이고 0 < tcpa < 10 min

선박 dict 형식: {"ship_id", "lat", "lon", "sog"(kn), "cog"(도, 진북 기준)}

데모: python route_conflict.py   # 90도 교차 조우 시나리오 타임라인
"""
from __future__ import annotations

import math


def _rel_nm(own: dict, tgt: dict) -> tuple[float, float, float, float]:
    """자선 기준 ENU 상대 상태 — 위치 (x동, y북, nm), 속도 (kn)."""
    kx = 60.0 * math.cos(math.radians((own["lat"] + tgt["lat"]) / 2.0))
    rx = (tgt["lon"] - own["lon"]) * kx
    ry = (tgt["lat"] - own["lat"]) * 60.0

    def vel(s: dict) -> tuple[float, float]:
        b = math.radians(s["cog"])
        return s["sog"] * math.sin(b), s["sog"] * math.cos(b)

    ovx, ovy = vel(own)
    tvx, tvy = vel(tgt)
    return rx, ry, tvx - ovx, tvy - ovy


def cpa_tcpa(own: dict, tgt: dict) -> tuple[float, float]:
    """(cpa_nm, tcpa_min). 서로 멀어지는 중이면 tcpa < 0 (cpa는 현재 거리)."""
    rx, ry, vx, vy = _rel_nm(own, tgt)
    v2 = vx * vx + vy * vy
    if v2 < 1e-9:                                   # 상대 정지: 거리 유지
        return math.hypot(rx, ry), 0.0
    t_h = -(rx * vx + ry * vy) / v2                 # 최근접 시점(시간)
    if t_h < 0.0:
        return math.hypot(rx, ry), t_h * 60.0
    return math.hypot(rx + vx * t_h, ry + vy * t_h), t_h * 60.0


def check_conflicts(own: dict, traffic: list[dict],
                    warn_cpa_nm: float = 0.5, warn_tcpa_min: float = 20.0,
                    danger_cpa_nm: float = 0.25, danger_tcpa_min: float = 10.0) -> list[dict]:
    """타선 목록에서 경고 대상만 추려 반환 (danger가 warn보다 우선)."""
    out: list[dict] = []
    for t in traffic:
        cpa, tcpa = cpa_tcpa(own, t)
        if tcpa <= 0.0:
            continue
        if cpa < danger_cpa_nm and tcpa < danger_tcpa_min:
            level = "danger"
        elif cpa < warn_cpa_nm and tcpa < warn_tcpa_min:
            level = "warn"
        else:
            continue
        out.append({"shipId": t.get("ship_id", "?"), "level": level,
                    "cpaNm": round(cpa, 2), "tcpaMin": round(tcpa, 1)})
    return out


# ---------------------------------------------------------------- 데모

def _step(s: dict, dt_min: float) -> dict:
    """등속 직진으로 dt_min 뒤 위치."""
    b = math.radians(s["cog"])
    d_nm = s["sog"] * dt_min / 60.0
    return {**s,
            "lat": s["lat"] + d_nm * math.cos(b) / 60.0,
            "lon": s["lon"] + d_nm * math.sin(b) / (60.0 * math.cos(math.radians(s["lat"])))}


if __name__ == "__main__":
    own = {"ship_id": "SHIP-01", "lat": 35.200, "lon": 130.600, "sog": 9.0, "cog": 85.0}
    tgt = {"ship_id": "SHIP-77", "lat": 35.155, "lon": 130.646, "sog": 12.0, "cog": 0.0}
    print("교차 조우 시나리오: SHIP-01 (동진 9kn) vs SHIP-77 (북진 12kn)\n")
    print(f"{'t(min)':>6} {'dist(nm)':>9} {'cpa(nm)':>8} {'tcpa(min)':>9}  판정")
    last = None
    for m in range(0, 31):
        rx, ry, _, _ = _rel_nm(own, tgt)
        cpa, tcpa = cpa_tcpa(own, tgt)
        found = check_conflicts(own, [tgt])
        level = found[0]["level"] if found else "-"
        if level != last or m % 5 == 0:
            print(f"{m:>6} {math.hypot(rx, ry):>9.2f} {cpa:>8.2f} {tcpa:>9.1f}  {level}")
            last = level
        own, tgt = _step(own, 1.0), _step(tgt, 1.0)
