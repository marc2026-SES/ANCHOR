#!/usr/bin/env python3
"""선단 접근 스캐너 — MEC013 Location API를 폴링해 전 선박 쌍의 CPA/TCPA를 감시.

선배 MEC 샌드박스(anchor-korea-strait)의 Location API에서 선박 위치를 주기 조회,
연속 위치로 속력·침로를 추정해 route_conflict로 쌍별 접근 경고를 계산한다.
fleet-ai(연안 MEC 앱)의 몸통이자, 데모 각본(언제 어느 쌍이 경고인지) 스캔 도구.

사용:
  python tools/fleet_scan.py --base-url https://<호스트>/<sbx>/<mep>/location/v2 \
      [--token <Bearer>] [--ca ca.pem] [--interval 2] [--time-scale 20]
  python tools/fleet_scan.py --once --base-url ...     # 선박 목록·위치 1회 확인
  python tools/fleet_scan.py --selftest                # 네트워크 없이 로직 검증

--time-scale: 샌드박스 배속(예: 20배속이면 20) — 속력·TCPA를 시나리오 시간으로 환산.
출력: 콘솔 경고 + results/fleet_scan.jsonl (이벤트), 30초마다 최소 CPA 상태줄.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from link_forecast import event_id  # noqa: E402
from onem2m_client import ACOP_ALL, OneM2MClient, OneM2MError  # noqa: E402
from route_conflict import check_conflicts, cpa_tcpa  # noqa: E402


def _num(v):
    """MEC013 구현에 따라 숫자가 리스트로 오기도 함."""
    if isinstance(v, list):
        v = v[0] if v else None
    return float(v) if isinstance(v, (int, float)) else None


def parse_users(doc: dict) -> list[dict]:
    """MEC013 UserList → [{"id", "lat", "lon"}] (locationInfo 없는 항목 제외)."""
    users = ((doc.get("userList") or {}).get("user")) or []
    out = []
    for u in users:
        loc = u.get("locationInfo") or {}
        lat, lon = _num(loc.get("latitude")), _num(loc.get("longitude"))
        if lat is None or lon is None:
            continue
        out.append({"id": str(u.get("address") or u.get("userId") or "?"),
                    "lat": lat, "lon": lon, "zone": str(u.get("zoneId") or "")})
    return out


def pair_owner(zone_a: str, zone_b: str, zone_map: dict[str, str]) -> str:
    """쌍의 경고 '발행 담당' — 모든 인스턴스가 전체를 계산하되 발행만 나눠
    중복·구역 경계 누락을 없앤다. 어느 엣지 구역에도 안 걸리면 cloud 담당."""
    for role, zone in zone_map.items():
        if zone and zone in (zone_a, zone_b):
            return role
    return "cloud"


class Track:
    """연속 위치로 속력(kn)·침로(도)를 추정."""

    def __init__(self):
        self.lat = self.lon = None
        self.t = None
        self.sog = 0.0
        self.cog = 0.0
        self.zone = ""

    def update(self, lat: float, lon: float, t: float, scale: float) -> None:
        if self.t is not None and t > self.t + 1e-3:
            kx = 60.0 * math.cos(math.radians((lat + self.lat) / 2.0))
            dx_nm = (lon - self.lon) * kx
            dy_nm = (lat - self.lat) * 60.0
            dt_h = (t - self.t) / 3600.0
            d_nm = math.hypot(dx_nm, dy_nm)
            if d_nm > 1e-6:
                self.sog = d_nm / dt_h / scale      # 시나리오 시간 기준 kn
                self.cog = math.degrees(math.atan2(dx_nm, dy_nm)) % 360.0
        self.lat, self.lon, self.t = lat, lon, t

    def ship(self, sid: str) -> dict:
        return {"ship_id": sid, "lat": self.lat, "lon": self.lon,
                "sog": self.sog, "cog": self.cog, "zone": self.zone}


def scan_pairs(ships: list[dict], warn_cpa, warn_tcpa, danger_cpa, danger_tcpa,
               zone_map: dict[str, str] | None = None):
    """모든 쌍 검사 → (경고 목록, 최소 CPA 쌍). zone_map이 있으면 쌍별 담당(owner) 표기."""
    alerts, closest = [], None
    for i in range(len(ships)):
        for j in range(i + 1, len(ships)):
            a, b = ships[i], ships[j]
            cpa, tcpa = cpa_tcpa(a, b)
            if closest is None or cpa < closest[2]:
                closest = (a["ship_id"], b["ship_id"], cpa, tcpa)
            for f in check_conflicts(a, [b], warn_cpa, warn_tcpa, danger_cpa, danger_tcpa):
                alert = {"pair": [a["ship_id"], b["ship_id"]],
                         "level": f["level"], "cpaNm": f["cpaNm"],
                         "tcpaMin": f["tcpaMin"]}
                if zone_map is not None:
                    alert["owner"] = pair_owner(a.get("zone", ""), b.get("zone", ""),
                                                zone_map)
                alerts.append(alert)
    return alerts, closest


def setup_cse(args) -> OneM2MClient | None:
    """연안 CSE에 fleetAI AE·컨테이너·조회 ACP 준비 — 결과를 oneM2M으로 공개."""
    if not args.cse_url:
        return None
    try:
        c = OneM2MClient(args.cse_url, args.cse_rn, args.cse_originator, rvi="3", timeout=8)
        c.ensure_ae("fleetAI", "NanchorFleet")
        c.ensure_container("fleetAI", "conflicts", mni=500)
        c.ensure_container("fleetAI", "fleetStatus", mni=200)
        readers = [r for r in (args.readers or "").split(",") if r]
        if readers:
            acp, _ = c.ensure_acp("fleetAI", "acpFleetAI",
                                  grants=[([args.cse_originator], ACOP_ALL),
                                          (readers, 34)],
                                  self_originators=[args.cse_originator])
            ri = str(c.resource_attr(acp, "ri"))
            for rn in ("conflicts", "fleetStatus"):
                res = c.retrieve(f"fleetAI/{rn}")
                if res is not None and not c.resource_attr(res, "acpi"):
                    c.update(f"fleetAI/{rn}", {"m2m:cnt": {"acpi": [ri]}})
        print(f"fleet-ai CSE 출력: {args.cse_url}/{args.cse_rn}/fleetAI "
              f"(조회 허용: {args.readers})")
        return c
    except (OneM2MError, OSError) as e:
        print(f"[warn] CSE 출력 준비 실패: {e} — 콘솔·jsonl만 기록")
        return None


def run_live(args) -> None:
    import requests

    s = requests.Session()
    if args.token:
        s.headers["Authorization"] = f"Bearer {args.token}"
    s.verify = args.ca if args.ca else (False if args.insecure else True)
    if args.insecure and not args.ca:
        print("[warn] TLS 검증 끔(--insecure) — 랩 내부 테스트 전용, CA 받으면 --ca 사용")
        requests.packages.urllib3.disable_warnings()

    zmap = (dict(kv.split("=", 1) for kv in args.zone_map.split(","))
            if args.zone_map else {})
    cse = setup_cse(args)
    out = ROOT / "results" / "fleet_scan.jsonl"
    out.parent.mkdir(exist_ok=True)
    f = out.open("a", encoding="utf-8")
    tracks: dict[str, Track] = {}
    active: set[tuple] = set()
    last_status = 0.0
    print(f"폴링 시작: {args.base_url} (interval {args.interval}s, "
          f"time-scale {args.time_scale}x, {args.minutes:g}분) -> {out.relative_to(ROOT)}")
    t_end = time.time() + args.minutes * 60.0
    while time.time() < t_end:
        tic = time.time()
        try:
            r = s.get(f"{args.base_url.rstrip('/')}/queries/users", timeout=8)
            r.raise_for_status()
            users = parse_users(r.json())
        except Exception as e:
            print(f"[warn] 조회 실패: {e} — 재시도")
            time.sleep(args.interval)
            continue
        now = time.time()
        for u in users:
            tr = tracks.setdefault(u["id"], Track())
            tr.update(u["lat"], u["lon"], now, args.time_scale)
            tr.zone = u.get("zone", "")
        ships = [t.ship(sid) for sid, t in tracks.items()
                 if t.t is not None and t.sog > 0.05 and now - t.t < 30.0]
        if args.once:
            print(f"선박 {len(users)}척:")
            for u in users:
                print(f"  {u['id']:<24} lat {u['lat']:.5f}  lon {u['lon']:.5f}")
            return
        alerts, closest = scan_pairs(ships, args.warn_cpa, args.warn_tcpa,
                                     args.danger_cpa, args.danger_tcpa, zone_map=zmap)
        cur = set()
        for a in alerts:
            if args.role != "all" and a.get("owner") != args.role:
                continue                           # 담당 아닌 쌍은 발행하지 않음
            key = (tuple(sorted(a["pair"])), a["level"])
            cur.add(key)
            if key not in active:                  # 신규/승격만 출력·기록
                iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
                ev = {"ts": iso, "type": "fleet_conflict", "role": args.role,
                      "eventId": event_id("flt", iso,
                                          f"{a['pair'][0]}-{a['pair'][1]}"), **a}
                print(f"{iso} {a['level'].upper():<6} {a['pair'][0]} ~ {a['pair'][1]} "
                      f"cpa={a['cpaNm']}nm tcpa={a['tcpaMin']}min "
                      f"(담당 {a.get('owner', '-')})")
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
                f.flush()
                if cse:
                    try:
                        cse.create_cin("fleetAI/conflicts", ev,
                                       lbl=["fleetConflict", a["level"]])
                    except (OneM2MError, OSError) as e:
                        print(f"[warn] conflicts CIN 실패: {e}")
        active = cur
        if now - last_status >= 30.0 and closest:
            last_status = now
            print(f"  [{len(ships)}척 감시] 최소 CPA: {closest[0]} ~ {closest[1]} "
                  f"{closest[2]:.2f}nm (tcpa {closest[3]:.1f}min)")
            if cse:
                iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
                st = {"ts": iso, "eventId": event_id("fst", iso), "role": args.role,
                      "ships": len(ships), "state": "online",
                      "closestPair": {"a": closest[0], "b": closest[1],
                                      "cpaNm": round(closest[2], 2),
                                      "tcpaMin": round(closest[3], 1)}}
                try:
                    cse.create_cin("fleetAI/fleetStatus", st, lbl=["fleetStatus"])
                except (OneM2MError, OSError) as e:
                    print(f"[warn] fleetStatus CIN 실패: {e}")
        time.sleep(max(0.0, args.interval - (time.time() - tic)))


def run_selftest() -> None:
    """합성 2척(90도 교차)을 트랙 추정→스캔 전체 경로로 검증 — 네트워크 불필요."""
    from route_conflict import _step

    a = {"ship_id": "ferry", "lat": 35.200, "lon": 130.600, "sog": 9.0, "cog": 85.0}
    b = {"ship_id": "ghost", "lat": 35.155, "lon": 130.646, "sog": 12.0, "cog": 0.0}
    tracks = {"ferry": Track(), "ghost": Track()}
    seen = []
    for m in range(0, 31):
        now = m * 60.0
        for s in (a, b):
            tracks[s["ship_id"]].update(s["lat"], s["lon"], now, 1.0)
        ships = [t.ship(sid) for sid, t in tracks.items() if t.sog > 0.05]
        if len(ships) == 2:
            alerts, closest = scan_pairs(ships, 0.5, 20.0, 0.25, 10.0)
            lv = alerts[0]["level"] if alerts else "-"
            if not seen or seen[-1] != lv:
                seen.append(lv)
                print(f"t={m:>2}min  level={lv:<6} cpa={closest[2]:.2f}nm "
                      f"tcpa={closest[3]:.1f}min (추정 sog {ships[0]['sog']:.1f}/{ships[1]['sog']:.1f}kn)")
        a, b = _step(a, 1.0), _step(b, 1.0)
    assert "warn" in seen and "danger" in seen, "경고 미발화 — 추정 로직 확인 필요"
    print("셀프테스트 통과: 위치만으로 속력·침로 추정 → warn → danger → 해제 재현")


def main() -> None:
    ap = argparse.ArgumentParser(description="선단 접근 스캐너 (상세: 파일 docstring)")
    ap.add_argument("--base-url", help="MEC013 base (…/location/v2)")
    ap.add_argument("--token", default=None, help="Authorization Bearer 토큰")
    ap.add_argument("--ca", default=None, help="TLS CA 인증서 경로")
    ap.add_argument("--insecure", action="store_true", help="TLS 검증 끔(랩 테스트 전용)")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--time-scale", type=float, default=1.0, help="샌드박스 배속(예: 20)")
    ap.add_argument("--warn-cpa", type=float, default=0.5)
    ap.add_argument("--warn-tcpa", type=float, default=20.0)
    ap.add_argument("--danger-cpa", type=float, default=0.25)
    ap.add_argument("--danger-tcpa", type=float, default=10.0)
    ap.add_argument("--minutes", type=float, default=5.0, help="스캔 길이(분) 후 자동 종료")
    ap.add_argument("--role", default="all", choices=["all", "mep1", "mep2", "cloud"],
                    help="경고 발행 담당 — 전체 계산 후 자기 담당 쌍만 발행 (기본 all)")
    ap.add_argument("--zone-map", default="mep1=kr-busan,mep2=jp-kyushu",
                    help="역할=zoneId 매핑 (나머지 구역은 cloud 담당)")
    ap.add_argument("--cse-url", default=None,
                    help="결과 CIN을 쓸 연안 CSE (예: http://100.96.155.4:30080)")
    ap.add_argument("--cse-rn", default="cse-edge")
    ap.add_argument("--cse-originator", default="CAnchorAgent")
    ap.add_argument("--readers", default="CAnchorDashboard",
                    help="fleetAI 조회(acop 34) 허용 originator (쉼표 구분)")
    ap.add_argument("--once", action="store_true", help="선박 목록 1회 출력")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        run_selftest()
        return
    if not args.base_url:
        raise SystemExit("--base-url 필요 (또는 --selftest)")
    run_live(args)


if __name__ == "__main__":
    main()
