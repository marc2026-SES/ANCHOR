#!/usr/bin/env python3
"""충돌 회피 데모: 어선 한 척을 다른 배의 항로 한가운데에 표류시킨다.

"통신이 끊겨 항로 위에서 떠다니는 어선" 상황을 만든다. 표류선이 대상선의 직선 구간 위에 있으므로
대상선은 반드시 그 위를 지나고(타이밍을 맞출 필요가 없다), fleet-ai는
  ① 표류선 항로 이탈 경고 → ② 대상선~표류선 danger → ③ 대상선 경로 변경 → ④ 지나간 뒤 복귀
순으로 반응한다. 전제: fleet-ai가 --reroute --route-models 로 돌고, 두 배의 항로 모델이 학습돼 있다.

사용:
  python tools/demo_rogue.py start              # 대상선·표류선 자동 선택, 표류 시작까지 진행
  python tools/demo_rogue.py start --dry-run    # 아무것도 바꾸지 않고 지금 가능한 조합만 확인
  python tools/demo_rogue.py start --target ship-001 --rogue fishing-02
  python tools/demo_rogue.py restore            # 표류선을 원래 구역·경로·속력으로 되돌림

표류선의 원래 경로는 results/demo_rogue_backup.json 에 두며, restore 전에는 start를 다시 받지 않는다.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
BACKUP = ROOT / "results" / "demo_rogue_backup.json"
FAST = 450            # 표류 지점까지·복귀할 때 속력 (m/s, 샌드박스 실시간)
DRIFT = 60            # 표류 속력 — 0이면 fleet-ai가 정지선으로 보고 감시에서 뺀다
DRIFT_KM = 6.0        # 항로 위에서 오가는 구간 길이


def km(a, b) -> float:
    return math.hypot((a[0] - b[0]) * 111.32 * math.cos(math.radians(a[1])), (a[1] - b[1]) * 110.57)


def ahead(pos, hx, hy, d):
    """pos에서 방향 (hx, hy)로 d km 간 지점 [lon, lat]."""
    return [round(pos[0] + hx * d / (111.32 * math.cos(math.radians(pos[1]))), 4),
            round(pos[1] + hy * d / 110.57, 4)]


class Map:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def ships(self) -> dict:
        ues = requests.get(f"{self.base}/api/geodata", timeout=8).json()["ues"]
        return {u["assetName"]: u for u in ues if u.get("velocity")}

    def route(self, ship: str, path: list, velocity=None, eop=None, allow_land=False) -> int:
        body = {"path": path, "by": "demo-rogue"}
        if velocity:
            body["velocity"] = velocity
        if eop:
            body["eopMode"] = eop
        if allow_land:
            body["allowLand"] = True
        return requests.post(f"{self.base}/api/ships/{ship}/route", json=body, timeout=8).status_code

    def wait_near(self, ship: str, point: list, near_km: float, timeout_s: float) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            try:
                if km(self.ships()[ship]["location"]["coordinates"], point) <= near_km:
                    return True
            except Exception:
                pass
            time.sleep(1.0)
        return False


def straight_leg(u: dict, hx: float, hy: float) -> float:
    """지금 침로 그대로 다음 경유점까지 남은 거리 km (직선 구간 위가 아니면 0)."""
    pos, best = u["location"]["coordinates"], 0.0
    for w in u["path"]["coordinates"]:
        d = km(pos, w)
        dx = (w[0] - pos[0]) * 111.32 * math.cos(math.radians(pos[1]))
        dy = (w[1] - pos[1]) * 110.57
        if d > 1.0 and (dx * hx + dy * hy) / d > 0.999:
            best = d if best == 0.0 else min(best, d)
    return best


def start(m: Map, want_target: str | None, want_rogue: str | None, exclude: set[str],
          wait_s: float, dry_run: bool = False) -> None:
    if BACKUP.exists():
        raise SystemExit(f"이미 표류 중인 배가 있음({json.loads(BACKUP.read_text())['ship']}) — 먼저 restore")
    t_begin, last_note = time.time(), 0.0
    while time.time() - t_begin < wait_s:
        a = m.ships(); time.sleep(4.0); b = m.ships()
        options = []                    # (여유 시간, 대상선, 표류선, 지점, 지점2, 남은 구간, 앞 거리, 표류선 도착, 대상선 도착)
        for t, u in b.items():
            if (want_target and t != want_target) or (not want_target and t.startswith("fishing")) \
                    or t in exclude or t not in a:
                continue
            p0, p1 = a[t]["location"]["coordinates"], u["location"]["coordinates"]
            d = km(p0, p1)
            if d <= 0:
                continue
            hx = (p1[0] - p0[0]) * 111.32 * math.cos(math.radians(p1[1])) / d
            hy = (p1[1] - p0[1]) * 110.57 / d
            leg = straight_leg(u, hx, hy)
            rogues = [r for r in b if r != t and r not in exclude and (r == want_rogue if want_rogue else True)]
            gap = 25.0
            while gap <= leg - DRIFT_KM - 8.0:          # 대상선 앞 어디에 세울지 5km 간격으로 탐색
                spot = ahead(p1, hx, hy, gap)
                t_target = gap * 1000.0 / u["velocity"]
                for r in rogues:
                    t_rogue = km(b[r]["location"]["coordinates"], spot) * 1000.0 / FAST
                    slack = t_target - t_rogue - 35.0   # 표류선이 먼저 닿고 fleet-ai가 이탈을 알아챌 여유
                    if r.startswith("fishing"):
                        slack += 15.0                   # 여유가 비슷하면 어선을 표류선으로 (이야기가 자연스럽다)
                    if slack > 0:
                        options.append((slack, t, r, spot, ahead(p1, hx, hy, gap + DRIFT_KM), leg, gap,
                                        t_rogue, t_target))
                gap += 5.0
        for slack, t, r, spot, spot2, leg, gap, t_rogue, t_target in sorted(options, key=lambda o: -o[0]):
            if dry_run:
                print(f"지금 가능: 대상선 {t} / 표류선 {r} — 대상선 앞 {gap:.0f}km {spot}, "
                      f"표류선 {t_rogue:.0f}초·대상선 {t_target:.0f}초 뒤 도착 (후보 {len(options)}개)")
                return
            ru = b[r]
            if m.route(r, [spot], velocity=FAST, eop="REVERSE") != 200:
                continue                                # 가는 길이 육지에 막힘 — 다음 후보
            BACKUP.parent.mkdir(exist_ok=True)
            BACKUP.write_text(json.dumps({"ship": r, "path": ru["path"]["coordinates"],
                                          "eopMode": ru["eopMode"], "velocity": ru["velocity"]}))
            print(f"대상선 {t} (직선 구간 {leg:.0f}km 남음) / 표류선 {r}")
            print(f"{r} → 대상선 앞 {gap:.0f}km 지점 {spot}으로 이동 중 (약 {t_rogue:.0f}초)")
            if not m.wait_near(r, spot, 1.0, t_rogue + 60.0):
                raise SystemExit("표류 지점에 닿지 못함 — restore 후 다시 시도")
            m.route(r, [spot, spot2], velocity=DRIFT, eop="REVERSE")
            print(f"표류 시작. {t}가 약 {max(0.0, t_target - t_rogue):.0f}초 뒤 이 지점을 지난다.")
            print("지도에서 볼 것: ① 표류선 항로 이탈 경고 ② 두 배 danger ③ 대상선 경로가 옆으로 꺾임 "
                  "④ 지나간 뒤 대상선이 원래 항로로 복귀")
            print("끝나면: python tools/demo_rogue.py restore")
            return
        if time.time() - last_note >= 20.0:
            last_note = time.time()
            print(f"  조건이 맞는 순간을 기다리는 중… ({time.time() - t_begin:.0f}초 경과)", flush=True)
    raise SystemExit("기다리는 동안 조건이 맞는 순간이 없었음 (긴 직선 구간의 대상선과 먼저 닿을 수 있는 어선 필요)")


def restore(m: Map) -> None:
    if not BACKUP.exists():
        raise SystemExit("표류 중인 배가 없음")
    bk = json.loads(BACKUP.read_text())
    ship, home = bk["ship"], bk["path"][0]
    if m.route(ship, [home], velocity=FAST, eop="REVERSE") != 200:
        m.route(ship, [home], velocity=FAST, eop="REVERSE", allow_land=True)
    print(f"{ship} → 원래 구역 {home}으로 복귀 중")
    if not m.wait_near(ship, home, 1.5, 1200.0):
        print("[warn] 20분 안에 닿지 못함 — 그 자리에서 원래 경로를 건다")
    else:
        # 마지막 구간은 원래 속력으로 다가가 첫 경유점 바로 위에서 경로를 건다. 덧붙는 점이 멀면
        # 그 구간을 돌 때마다 다른 침로로 지나게 되어 항로 이탈 경고가 반복된다.
        m.route(ship, [home], velocity=bk["velocity"], eop="REVERSE")
        near = min(0.5, max(0.08, bk["velocity"] * 1.5 / 1000.0))
        if not m.wait_near(ship, home, near, 300.0):
            print("[warn] 첫 경유점에 정밀하게 닿지 못함 — 가까운 자리에서 원래 경로를 건다")
    if m.route(ship, bk["path"], velocity=bk["velocity"], eop=bk["eopMode"]) != 200:
        m.route(ship, bk["path"], velocity=bk["velocity"], eop=bk["eopMode"], allow_land=True)
    BACKUP.unlink()
    u = m.ships()[ship]
    print(f"{ship} 원래 경로 복구: {u['eopMode']} v={u['velocity']} 경유점 {len(u['path']['coordinates'])}개")


def main() -> None:
    ap = argparse.ArgumentParser(description="충돌 회피 데모 — 표류선 시나리오 (상세: 파일 docstring)")
    ap.add_argument("cmd", choices=["start", "restore"])
    ap.add_argument("--map-url", default="http://100.96.155.4:30793")
    ap.add_argument("--target", default=None, help="피하게 될 배 (기본: 직선 구간이 긴 배 자동 선택)")
    ap.add_argument("--rogue", default=None, help="표류시킬 배 (기본: 먼저 닿을 수 있는 배 중 자동, 어선 우선)")
    ap.add_argument("--dry-run", action="store_true", help="아무것도 바꾸지 않고 지금 가능한 조합만 출력")
    ap.add_argument("--wait-min", type=float, default=10.0, help="조건이 맞는 순간을 기다리는 최대 시간(분)")
    ap.add_argument("--exclude", default="", help="대상선에서 뺄 배 (쉼표 구분) — 항로 재학습 전인 배 등")
    args = ap.parse_args()
    m = Map(args.map_url)
    if args.cmd == "start":
        start(m, args.target, args.rogue, {x for x in args.exclude.split(",") if x}, args.wait_min * 60.0,
              args.dry_run)
    else:
        restore(m)


if __name__ == "__main__":
    main()
