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
--hazard-url: 지도 서버의 재해 목록(/api/hazards)을 읽어 태풍·지진해일 구역에 든 배(danger)와
  곧 들어갈 배(진입 2시간 전 warn, 30분 전 danger — 시나리오 시간)를 type=hazard_warning 으로
  같은 conflicts 컨테이너에 발행. 태풍은 진로에서 멀어지는 권고 침로를 포함. 읽기 전용이며
  배의 경로는 바꾸지 않는다.
--map-url + --reroute: 지도 서버의 경로 변경 API(POST /api/ships/<배>/route)로 태풍 구역에
  들었거나 들어갈 배를 진로 밖으로 실제로 보낸다(육지를 지나는 경로는 서버가 거부 → 반대쪽 시도).
  재해가 목록에서 사라지면 원래 경로로 되돌린다. 지진해일은 경고만 한다.
충돌 회피(--reroute + --route-models): 최근접 0.2nm·8분 이내가 2회 연속이고 상대 배가 학습된
  항로를 벗어나 있을 때만, 항로를 지키고 있는 배의 경로를 상대 뒤쪽으로 바꾼다(type=
  conflict_avoidance). 서로 멀어져 3nm 벌어지면 원래 경로로 복귀한다.
--track-log: 정상 항적(재해 없음·대피/복귀 중 아님)을 JSONL로 남긴다 — 항로 학습 데이터.
--route-models: fleet_route_model.py 로 학습한 배별 항로 모델로 항로 이탈(type=route_deviation)을
  경고한다. 연속 3회 벗어나야 발행하고, AI가 경로를 건드린 배는 재학습 전까지 판정을 보류한다.
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


def _first_hit(px: float, py: float, wx: float, wy: float, r0: float, grow: float):
    """상대 위치 p(km)·상대 속도 w(km/h)인 점이 반지름 r0 + grow·t 인 원에 처음 닿는 시각 t(h).
    닿지 않으면 None."""
    a = wx * wx + wy * wy - grow * grow
    b = 2.0 * (px * wx + py * wy - r0 * grow)
    c = px * px + py * py - r0 * r0
    if c <= 0:                      # 이미 원 안 — '진입 예정'이 아니다
        return None
    if abs(a) < 1e-9:
        return -c / b if abs(b) > 1e-9 and -c / b > 0 else None
    disc = b * b - 4.0 * a * c
    if disc < 0:
        return None
    roots = sorted(((-b - math.sqrt(disc)) / (2.0 * a), (-b + math.sqrt(disc)) / (2.0 * a)))
    return next((t for t in roots if t > 0), None)


def hazard_alerts(ships: list[dict], hazards: list[dict], scale: float,
                  warn_min: float, danger_min: float) -> list[dict]:
    """재해 구역에 이미 든 배와 곧 들어갈 배를 찾는다 (지도 서버 /api/hazards 형식).

    태풍은 침로·속력으로 이동하는 원, 지진해일은 진앙에서 퍼지는 파면으로 보고, 배의 추정
    속력·침로를 직선 투영해 만나는 시각을 구한다. 재해 속도와 남은 시간은 샌드박스 실시간,
    배의 sog는 시나리오 시간이라 scale(배속)로 맞추고, 결과 etaMin은 충돌 경고의 tcpaMin과
    같은 시나리오 분이다. 태풍은 진로에서 멀어지는 방향(진로에 수직)의 권고 침로를 함께 낸다.
    """
    out = []
    for h in hazards:
        try:
            hlon, hlat = float(h["center"][0]), float(h["center"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        typhoon = h.get("type") == "typhoon"
        remain_h = float(h.get("remainingSec") or 0) / 3600.0
        inside_now = set(h.get("ships") or [])
        kx = 111.32 * math.cos(math.radians(hlat))
        for s in ships:
            px, py = (s["lon"] - hlon) * kx, (s["lat"] - hlat) * 110.57
            v = s["sog"] * 1.852 * scale                                   # 샌드박스 실시간 km/h
            vx, vy = v * math.sin(math.radians(s["cog"])), v * math.cos(math.radians(s["cog"]))
            escape = None
            if typhoon:
                hd = math.radians(float(h.get("headingDeg") or 0))
                sp = float(h.get("speedKmh") or 0)
                t = _first_hit(px, py, vx - sp * math.sin(hd), vy - sp * math.cos(hd),
                               float(h.get("radiusKm") or 0), 0.0)
                right = px * math.cos(hd) - py * math.sin(hd) >= 0         # 태풍 진로의 오른쪽인가
                escape = round((float(h.get("headingDeg") or 0) + (90 if right else -90)) % 360)
            else:
                front, sp = float(h.get("waveRadiusKm") or 0), float(h.get("speedKmh") or 0)
                t = _first_hit(px, py, vx, vy, front, sp)
                if t is not None and front + sp * t > float(h.get("maxRadiusKm") or 0):
                    t = None                                                # 최대 확산 반경 밖
            if s["ship_id"] in inside_now:
                eta, level = 0.0, "danger"
            elif t is None or t > remain_h:
                continue
            else:
                eta = t * 60.0 * scale
                if eta > warn_min:
                    continue
                level = "danger" if eta <= danger_min else "warn"
            alert = {"ship": s["ship_id"], "hazardId": str(h.get("id", "")),
                     "hazardType": str(h.get("type", "")), "hazardName": str(h.get("name", "")),
                     "level": level, "etaMin": round(eta, 1), "inside": eta == 0.0,
                     "zone": s.get("zone", "")}
            if escape is not None:
                alert["escapeCourseDeg"] = escape
            out.append(alert)
    return out


def typhoon_escape_routes(ship: dict, hz: dict) -> list[list[list[float]]]:
    """태풍 진로 옆으로 비켜서는 대피 경로 후보 [[lon, lat], [lon, lat]] — 우선순위 순.

    첫 점은 진로에서 (반경 + 여유)만큼 옆으로 벗어난 지점, 둘째 점은 같은 방향으로 60km 더
    나간 지점이라 두 점 사이를 오가는 동안은 진로 밖에 머문다. 좁은 해협에서는 섬·해안에
    막히는 후보가 많으므로 배와 같은 쪽 → 반대쪽, 여유 20/45/80km, 진로 방향 0/−50/+50/−100km
    이동을 조합해 여러 개를 낸다. 육지 여부는 여기서 보지 않는다(지도 API가 판정).
    """
    hlon, hlat = float(hz["center"][0]), float(hz["center"][1])
    kx, ky = 111.32 * math.cos(math.radians(hlat)), 110.57
    hd = math.radians(float(hz.get("headingDeg") or 0))
    hx, hy = math.sin(hd), math.cos(hd)             # 태풍 진행 방향
    nx, ny = hy, -hx                                # 그 오른쪽
    px, py = (ship["lon"] - hlon) * kx, (ship["lat"] - hlat) * ky
    along, lateral = px * hx + py * hy, px * nx + py * ny
    radius = float(hz.get("radiusKm") or 0)

    def point(a: float, off: float) -> list[float]:
        return [round(hlon + (hx * a + nx * off) / kx, 3), round(hlat + (hy * a + ny * off) / ky, 3)]

    routes = []
    for side in ((1, -1) if lateral >= 0 else (-1, 1)):
        for extra in (20.0, 45.0, 80.0):
            for shift in (0.0, -50.0, 50.0, -100.0):
                off = side * (radius + extra)
                routes.append([point(along + shift, off), point(along + shift, off + side * 60.0)])
    return routes


SETTLE_S = 1800.0         # 경로 복귀 후 이 시간 동안은 항적을 '정상'으로 기록하지 않는다
RELEARN_S = 3600.0        # 경로를 건드린 배는 그 뒤 이만큼 지나 학습한 모델이 생길 때까지 이탈 판정 보류
DEVIATION_SCANS = 3       # 연속 이 횟수만큼 벗어나 있어야 이탈로 본다 (순간 튐 무시)
AVOID_SCANS = 2           # 충돌 회피 기준을 연속 이 횟수만큼 만족해야 경로를 바꾼다
CONFLICT_CLEAR_NM = 3.0   # 충돌 회피 후 서로 멀어지며 이만큼 벌어지면 원래 경로로 복귀
CONFLICT_HOLD_MAX_S = 600.0   # 그래도 안 풀리면 이 시간 뒤 복귀 (샌드박스 실시간)
SCAN_S = 2.0              # 복귀 정밀도 계산에 쓰는 관측 간격(초)
RETURN_MAX_S = 1500.0     # 그때까지 못 닿으면 그 자리에서 원래 경로를 건다
MAX_ROUTE_POSTS = 12      # 배 한 척의 대피 경로를 찾을 때 경로 변경 요청 상한


def avoidance_picks(alerts: list[dict], off_route: dict, runs: dict,
                    avoid_cpa: float, avoid_tcpa: float) -> list[tuple[str, str, dict]]:
    """충돌 경고 중 경로를 바꿔야 하는 것만 고른다 → [(피할 배, 항로를 벗어난 배, 경고)].

    조건 셋을 모두 만족해야 한다: ① danger이고 최근접 거리·시간이 회피 기준(danger보다 엄격) 안,
    ② 그 상태가 AVOID_SCANS회 연속, ③ 둘 중 정확히 한 척만 학습된 항로를 벗어나 있음.
    둘 다 평소 항로면 직진 가정이 틀린 경우가 대부분이고(곧 경유점에서 튼다), 둘 다 벗어났으면
    지시를 받을 배가 없다. runs(쌍 → 연속 횟수)는 이 함수가 갱신한다.
    """
    picks, seen = [], set()
    for a in alerts:
        pk = tuple(sorted(a["pair"]))
        seen.add(pk)
        hit = (a["level"] == "danger" and a["cpaNm"] < avoid_cpa and 0.0 <= a["tcpaMin"] < avoid_tcpa)
        runs[pk] = runs.get(pk, 0) + 1 if hit else 0
        strays = [sid for sid in a["pair"] if sid in off_route]
        if runs[pk] >= AVOID_SCANS and len(strays) == 1:
            stray = strays[0]
            picks.append((a["pair"][0] if a["pair"][1] == stray else a["pair"][1], stray, a))
    for pk in [k for k in runs if k not in seen]:
        del runs[pk]
    return picks


def conflict_avoid_routes(own: dict, other: dict, tcpa_min: float) -> list[list[list[float]]]:
    """충돌을 피하는 우회 경로 후보 [[lon, lat], [lon, lat]] — 우선순위 순.

    이대로 가면 만나는 지점(자기 침로로 tcpa 동안 간 곳)을 기준으로, 침로가 엇갈리면 상대가
    지나온 쪽(선미 뒤)으로 4/7/11km 비켜 지나가고, 거의 나란하면(마주 오거나 같은 방향) 오른쪽 →
    왼쪽으로 비킨다. 둘째 점은 첫 점에서 자기 침로로 20km 더 간 곳이라 비킨 뒤 그대로 통과한다.
    """
    kx, ky = 111.32 * math.cos(math.radians(own["lat"])), 110.57
    hx, hy = math.sin(math.radians(own["cog"])), math.cos(math.radians(own["cog"]))
    gx, gy = math.sin(math.radians(other["cog"])), math.cos(math.radians(other["cog"]))
    run = own["sog"] * 1.852 * max(tcpa_min, 0.0) / 60.0
    cx, cy = hx * run, hy * run
    dirs = [(hy, -hx), (-hy, hx)]                   # 오른쪽, 왼쪽
    if abs(hx * gy - hy * gx) >= 0.35:              # 침로가 20도 이상 엇갈림 → 상대 뒤로
        dirs.insert(0, (-gx, -gy))
    routes = []
    for dx, dy in dirs:
        for off in (4.0, 7.0, 11.0):
            x, y = cx + dx * off, cy + dy * off
            routes.append([[round(own["lon"] + x / kx, 3), round(own["lat"] + y / ky, 3)],
                           [round(own["lon"] + (x + hx * 20.0) / kx, 3),
                            round(own["lat"] + (y + hy * 20.0) / ky, 3)]])
    return routes


class Rerouter:
    """지도 서버의 경로 변경 API로 배를 태풍 진로 밖으로 보내고, 재해가 끝나면 원래 경로로 돌린다.

    아직 복귀시키지 못한 배의 원래 경로는 results/route_moved.json 에 두어 스캐너가 재시작돼도
    이어서 복귀시킨다. results/route_backup.jsonl 은 바꾼 이력(수동 복구용 기록)이다.
    """

    def __init__(self, map_url: str):
        import requests
        self.http = requests
        self.base = map_url.rstrip("/")
        self._land: dict[tuple, bool] = {}
        self.settling: dict[str, float] = {}        # 배 → 이 시각까지는 원래 항로로 돌아가는 중
        self.changed_file = ROOT / "results" / "route_changed.json"
        self.changed: dict[str, float] = {}         # 배 → 마지막으로 경로를 건드린 시각
        if self.changed_file.exists():
            try:
                self.changed = json.loads(self.changed_file.read_text(encoding="utf-8"))
            except ValueError:
                pass
        self.backup = ROOT / "results" / "route_backup.jsonl"
        self.state = ROOT / "results" / "route_moved.json"
        self.moved: dict[str, dict] = {}            # 배 → {hazardId, path, eopMode, velocity}
        if self.state.exists():
            try:
                self.moved = json.loads(self.state.read_text(encoding="utf-8"))
                if self.moved:
                    print(f"복귀 대기 중인 배 {len(self.moved)}척 이어받음: {', '.join(self.moved)}")
            except ValueError:
                pass
        self.settling = {sid: ts + SETTLE_S for sid, ts in self.changed.items()}

    def _save(self) -> None:
        self.state.write_text(json.dumps(self.moved, ensure_ascii=False), encoding="utf-8")
        self.changed_file.write_text(json.dumps(self.changed), encoding="utf-8")

    def _post(self, ship_id: str, path: list, eop=None, velocity=None, allow_land: bool = False):
        body = {"path": path, "by": "fleet-ai"}
        if allow_land:
            body["allowLand"] = True
        if eop:
            body["eopMode"] = eop
        if velocity:
            body["velocity"] = velocity
        return self.http.post(f"{self.base}/api/ships/{ship_id}/route", json=body, timeout=6)

    def _is_land(self, pt: list[float]) -> bool:
        key = (pt[0], pt[1])
        if key not in self._land:
            r = self.http.get(f"{self.base}/api/land", params={"lon": pt[0], "lat": pt[1]}, timeout=4)
            self._land[key] = bool(r.json().get("land"))
        return self._land[key]

    def avoid(self, ship: dict, routes: list, reason: dict) -> list | None:
        """후보 경로 중 처음 받아들여지는 것으로 변경. 성공하면 그 경로, 못 바꾸면 None.
        reason: 복귀 판단에 쓰는 정보 — 태풍 {"hazardId"}, 충돌 {"kind": "conflict", "other", "since"}.

        경유점이 육지인 후보는 건너뛰고, 구간이 육지를 지나 서버가 거부하면 다음 후보를 시도한다.
        이미 대피시킨 배가 다시 경고되면(왕복하다 진로로 돌아온 경우) 원래 경로 백업은 그대로 두고
        현재 위치 기준으로 다시 보낸다.
        """
        sid = ship["ship_id"]
        try:
            saved = None
            if sid in self.moved:
                self.moved[sid].update({"hazardId": "", "kind": None, **reason})
                self.moved[sid].pop("returning", None)
            else:
                ues = self.http.get(f"{self.base}/api/geodata", timeout=6).json().get("ues") or []
                ue = next((u for u in ues if u.get("assetName") == sid), None)
                orig = ((ue or {}).get("path") or {}).get("coordinates")
                if not orig:
                    return None
                seg = self._nearest_segment([ship["lon"], ship["lat"]], orig)
                (ax, ay), (bx, by) = orig[seg], orig[seg + 1]
                along = ((bx - ax) * math.cos(math.radians(ay)) * math.sin(math.radians(ship["cog"]))
                         + (by - ay) * math.cos(math.radians(ship["cog"])))
                saved = {"hazardId": "", **reason, "path": orig, "fwd": along >= 0,
                         "eopMode": ue.get("eopMode"), "velocity": ue.get("velocity")}
            posts, last = 0, ""
            for route in routes:
                if self._is_land(route[0]):
                    continue
                if len(route) > 1 and self._is_land(route[1]):
                    route = route[:1]
                if posts >= MAX_ROUTE_POSTS:
                    break
                posts += 1
                r = self._post(sid, route)
                if r.status_code == 200:
                    if saved:
                        with self.backup.open("a", encoding="utf-8") as bf:
                            bf.write(json.dumps({"ship": sid, "ts": time.time(), **saved}) + "\n")
                        self.moved[sid] = saved
                    self._save()
                    return route
                last = f"{r.status_code} {r.text[:120]}"
            print(f"[warn] {sid} 대피 경로를 찾지 못함 (시도 {posts}회, 마지막 응답 {last}) — 경고만 발행")
        except Exception as e:
            print(f"[warn] {sid} 경로 변경 실패: {e}")
        return None

    def _position(self, sid: str) -> list | None:
        try:
            ues = self.http.get(f"{self.base}/api/geodata", timeout=6).json().get("ues") or []
            return next(u for u in ues if u.get("assetName") == sid)["location"]["coordinates"]
        except Exception:
            return None

    @staticmethod
    def _km(a: list, b: list) -> float:
        return math.hypot((a[0] - b[0]) * 111.32 * math.cos(math.radians(a[1])), (a[1] - b[1]) * 110.57)

    def _post_original(self, sid: str, st: dict) -> bool:
        """원래 경로·속력을 건다. 원래 경로가 해안선 판정에 걸리는 배(항구 접근 구간)는 판정을 건너뛴다 —
        새 경로가 아니라 시나리오의 원래 경로를 되돌리는 것이므로."""
        path = st["path"] if st.get("returning", 0) == 0 else st["path"][::-1]
        for allow in (False, True):
            if self._post(sid, path, st["eopMode"], st["velocity"], allow_land=allow).status_code == 200:
                return True
        return False

    def _nearest_segment(self, pos: list, path: list) -> int:
        """pos에서 가장 가까운 구간의 번호 i (path[i]~path[i+1])."""
        best, where = 1e18, 0
        kx = math.cos(math.radians(pos[1]))
        for i in range(len(path) - 1):
            ax, ay = (path[i][0] - pos[0]) * kx, path[i][1] - pos[1]
            bx, by = (path[i + 1][0] - pos[0]) * kx, path[i + 1][1] - pos[1]
            dx, dy = bx - ax, by - ay
            t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
            d = (ax + t * dx) ** 2 + (ay + t * dy) ** 2
            if d < best:
                best, where = d, i
        return where

    def _start_return(self, sid: str, st: dict) -> None:
        """복귀 1단계: 가던 방향 그대로 앞쪽 경유점으로 합류해 원래 경로의 끝점까지 간다.

        지도 API는 경로를 걸 때 현재 위치를 맨 앞에 붙인다. 그 자리에서 바로 원래 경로를 걸면 엉뚱한
        점이 경로에 영구히 남으므로, 끝점에 닿은 뒤 다시 걸어 덧붙는 점이 끝점과 겹치게 한다.
        뒤쪽(지나온 쪽)으로 되돌리면 방금 피한 상대를 다시 거슬러 지나게 되므로 앞쪽으로 보낸다.
        """
        path, pos = st["path"], self._position(sid)
        if pos is not None:
            seg, fwd, loop = self._nearest_segment(pos, path), st.get("fwd", True), st["eopMode"] == "LOOP"
            for allow in (False, True):
                for step in (0, 1, 2):                      # 합류 구간이 육지에 막히면 그다음 경유점으로
                    if fwd:
                        k = min(seg + 1 + step, len(path) - 1)
                        back, end = (path[k:] + [path[0]], 0) if loop else (path[k:], len(path) - 1)
                    else:
                        k = max(seg - step, 0)
                        back, end = path[k::-1], 0
                    if self._post(sid, back, "REVERSE", st["velocity"], allow_land=allow).status_code == 200:
                        st["returning"], st["return_since"] = end, time.time()
                        self._save()
                        print(f"원래 항로로 복귀 시작: {sid} (경유점 {k}번으로 합류해 "
                              f"{'시작' if end == 0 else '끝'}점까지)")
                        return
        st["returning"], st["return_since"] = 0, 0.0      # 돌아갈 길을 못 잡음 — 그 자리에서 바로 건다

    def restore_finished(self, live_hazard_ids: set[str], conflict_done: set[str]) -> None:
        """재해가 끝났거나(태풍) 상대와 충분히 벌어진(충돌 회피) 배를 원래 경로로 돌린다 (2단계)."""
        for sid, st in list(self.moved.items()):
            try:
                if st.get("returning") is None:
                    if st.get("kind") == "conflict":
                        if sid not in conflict_done:
                            continue
                    elif st["hazardId"] in live_hazard_ids:
                        continue
                    self._start_return(sid, st)
                    continue
                pos = self._position(sid)
                end = st["path"][0 if st["returning"] == 0 else -1]
                late = time.time() - st.get("return_since", 0.0) >= RETURN_MAX_S
                # 끝점에 '한 번 관측하는 동안 가는 거리' 안으로 들어오면 원래 경로를 건다. 덧붙는 점이
                # 끝점에서 멀수록 그 구간을 다른 침로로 지나는 시간이 길어져 항로 이탈로 잡히므로,
                # 느린 배일수록 가까이(어선 50m/s → 80m) 맞춘다.
                near_km = min(0.6, max(0.08, float(st["velocity"] or 300) * SCAN_S * 0.75 / 1000.0))
                if not late and (pos is None or self._km(pos, end) > near_km):
                    continue
                ok = self._post_original(sid, st)
                if ok:
                    print(f"원래 경로 복귀 완료: {sid}" + (" (끝점에 닿기 전 시간 초과)" if late else ""))
                else:
                    print(f"[warn] {sid} 원래 경로 복귀 거부 — {self.backup.name} 로 수동 복구 필요")
                del self.moved[sid]
                if late or not ok:          # 끝점에서 건 경우는 경로가 원래와 같아 보류할 이유가 없다
                    self.settling[sid] = time.time() + SETTLE_S
                    self.changed[sid] = time.time()
                self._save()
            except Exception as e:
                print(f"[warn] {sid} 원래 경로 복귀 실패: {e} — 다음 주기에 재시도")


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


class CseOutput:
    """연안 CSE 발행기. 준비 실패(부팅 직후 네트워크 미기동)와 CSE 초기화(404)에서 스스로 복구한다."""

    RETRY_S = 30.0

    def __init__(self, args):
        self.args = args
        self.client = setup_cse(args)
        self.retry_at = time.time() + self.RETRY_S

    def _ready(self) -> bool:
        if self.client is None and self.args.cse_url and time.time() >= self.retry_at:
            self.retry_at = time.time() + self.RETRY_S
            self.client = setup_cse(self.args)
        return self.client is not None

    def publish(self, rn: str, body: dict, lbl: list[str]) -> None:
        for attempt in (1, 2):
            if not self._ready():
                return
            try:
                self.client.create_cin(f"fleetAI/{rn}", body, lbl=lbl)
                return
            except OneM2MError as e:
                print(f"[warn] {rn} CIN 실패: {e}")
                if e.status != 404 or attempt == 2:
                    return
                self.client = None          # fleetAI가 사라짐(CSE 초기화) — 다시 만들고 1회 재전송
                self.retry_at = 0.0
            except OSError as e:
                print(f"[warn] {rn} CIN 실패: {e}")
                return


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
    cse = CseOutput(args)
    out = ROOT / "results" / "fleet_scan.jsonl"
    out.parent.mkdir(exist_ok=True)
    f = out.open("a", encoding="utf-8")
    tracks: dict[str, Track] = {}
    active: set[tuple] = set()
    last_status = 0.0
    hazard_warned = -1e9
    map_url = (args.map_url or "").rstrip("/")
    hazard_url = args.hazard_url or (f"{map_url}/api/hazards" if map_url else None)
    rerouter = Rerouter(map_url) if args.reroute and map_url else None
    track_log = open(args.track_log, "a", encoding="utf-8") if args.track_log else None
    routes = None
    if args.route_models:
        from fleet_route_model import RouteModels
        routes = RouteModels(args.route_models)
        print(f"항로 모델: {routes.version or '아직 없음'} ({len(routes.ships)}척) — {args.route_models}")
    off_runs: dict[str, list[int]] = {}             # 배 → [warn 연속 횟수, danger 연속 횟수]
    avoid_runs: dict[tuple, int] = {}               # 쌍 → 충돌 회피 기준 연속 만족 횟수
    last_reload = 0.0
    if args.reroute and not map_url:
        print("[warn] --reroute 는 --map-url 이 있어야 동작 — 경고만 발행")
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
        ship_by_id = {sh["ship_id"]: sh for sh in ships}
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
                cse.publish("conflicts", ev, ["fleetConflict", a["level"]])
        hazards = None                             # None = 이번 주기에 재해 목록을 못 읽음
        if hazard_url:
            try:
                hazards = (requests.get(hazard_url, timeout=4).json() or {}).get("hazards") or []
            except Exception as e:
                if now - hazard_warned >= 60.0:
                    hazard_warned = now
                    print(f"[warn] 재해 목록 조회 실패: {e}")
        try:                                       # 재해 처리의 예기치 못한 오류로 스캐너가 죽지 않게
            if hazards is not None:
                if rerouter:
                    done = set()
                    for sid, st in rerouter.moved.items():
                        if st.get("kind") != "conflict" or st.get("returning") is not None:
                            continue
                        me, it = ship_by_id.get(sid), ship_by_id.get(st["other"])
                        if now - st["since"] >= CONFLICT_HOLD_MAX_S:
                            done.add(sid)
                        elif me and it:
                            gap = math.hypot((me["lon"] - it["lon"]) * 60.0 * math.cos(math.radians(me["lat"])),
                                             (me["lat"] - it["lat"]) * 60.0)
                            if cpa_tcpa(me, it)[1] < 0 and gap >= CONFLICT_CLEAR_NM:
                                done.add(sid)
                    rerouter.restore_finished({str(h.get("id", "")) for h in hazards}, done)
                hz_by_id = {str(h.get("id", "")): h for h in hazards}
                for h in hazard_alerts(ships, hazards, args.time_scale,
                                       args.hazard_warn_min, args.hazard_danger_min):
                    owner = pair_owner(h.pop("zone", ""), "", zmap)
                    if args.role != "all" and owner != args.role:
                        continue
                    key = (h["ship"], h["hazardId"], h["level"])
                    cur.add(key)
                    if key in active:
                        continue
                    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
                    # pair·tcpaMin은 충돌 경고와 같은 자리에 표시되도록 맞춘 필드 (지도·대시보드 공용)
                    ev = {"ts": iso, "type": "hazard_warning", "role": args.role,
                          "eventId": event_id("hzd", iso, f"{h['ship']}-{h['hazardId']}"),
                          "pair": [h["ship"], h["hazardName"] or h["hazardType"]],
                          "tcpaMin": h["etaMin"], "owner": owner, **h}
                    where = "구역 안" if h["inside"] else f"{h['etaMin']}분 뒤 진입"
                    advice = (f", 권고 침로 {h['escapeCourseDeg']}°"
                              if "escapeCourseDeg" in h and not h["inside"] else "")
                    if rerouter and h["hazardType"] == "typhoon":
                        hz = hz_by_id[h["hazardId"]]
                        route = rerouter.avoid(ship_by_id[h["ship"]],
                                               typhoon_escape_routes(ship_by_id[h["ship"]], hz),
                                               {"hazardId": str(hz.get("id", ""))})
                        if route:
                            ev["action"], ev["escapeRoute"] = "rerouted", route
                            advice = ", 경로 변경 → " + " → ".join(str(pt) for pt in route)
                    print(f"{iso} {h['level'].upper():<6} {h['ship']} → {h['hazardType']} "
                          f"'{h['hazardName']}' {where}{advice} (담당 {owner})")
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
                    f.flush()
                    cse.publish("conflicts", ev, ["fleetHazard", h["level"]])
        except Exception as e:
            print(f"[warn] 재해 처리 오류: {type(e).__name__}: {e} — 충돌 감시는 계속")
        off_route: dict[str, str] = {}             # 이번 주기에 항로를 벗어나 있는 배 → 등급
        if routes:
            try:
                if now - last_reload >= 30.0:
                    last_reload = now
                    if routes.reload():
                        print(f"항로 모델 적용: {routes.version} ({len(routes.ships)}척)")
                for sh in ships:
                    sid = sh["ship_id"]
                    if rerouter and (sid in rerouter.moved or now < rerouter.settling.get(sid, 0.0)
                                     or routes.trained_at < rerouter.changed.get(sid, 0.0) + RELEARN_S):
                        off_runs.pop(sid, None)        # 대피 중·복귀 중·경로가 바뀐 뒤 재학습 전
                        continue
                    res = routes.check(sid, sh["lat"], sh["lon"], sh["cog"])
                    if res is None:
                        continue
                    dist, lv = res
                    run = off_runs.setdefault(sid, [0, 0])
                    run[0] = run[0] + 1 if lv != "normal" else 0
                    run[1] = run[1] + 1 if lv == "danger" else 0
                    level = ("danger" if run[1] >= DEVIATION_SCANS
                             else "warn" if run[0] >= DEVIATION_SCANS else None)
                    if level is None:
                        continue
                    off_route[sid] = level
                    owner = pair_owner(sh.get("zone", ""), "", zmap)
                    if args.role != "all" and owner != args.role:
                        continue
                    key = (sid, "route", level)
                    cur.add(key)
                    if key in active:
                        continue
                    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
                    ev = {"ts": iso, "type": "route_deviation", "role": args.role,
                          "eventId": event_id("rte", iso, sid), "pair": [sid, "off-route"],
                          "ship": sid, "level": level, "offRouteKm": round(dist, 1),
                          "modelVersion": routes.version, "owner": owner}
                    print(f"{iso} {level.upper():<6} {sid} 항로 이탈 — 학습된 항적에서 {dist:.1f}km "
                          f"(모델 {routes.version}, 담당 {owner})")
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
                    f.flush()
                    cse.publish("conflicts", ev, ["fleetRoute", level])
            except Exception as e:
                print(f"[warn] 항로 이탈 판정 오류: {type(e).__name__}: {e} — 충돌 감시는 계속")
        # 충돌 회피: 엄격한 기준을 연속으로 만족하고, 상대가 학습된 항로를 벗어나 있을 때만 경로를 바꾼다.
        # 둘 다 평소 항로면 직진 가정이 틀린 경우가 대부분이고(곧 경유점에서 튼다), 둘 다 벗어났으면
        # 지시를 받을 배가 없다. 피하는 쪽은 항로를 지키고 있는 배.
        if rerouter and routes and routes.ships:
            try:
                for yielding, stray, a in avoidance_picks(alerts, off_route, avoid_runs,
                                                          args.avoid_cpa, args.avoid_tcpa):
                    held = rerouter.moved.get(yielding)
                    if (held and held.get("returning") is None) \
                            or now < rerouter.settling.get(yielding, 0.0):
                        continue                         # 이미 피하는 중 (복귀 중이면 다시 피할 수 있다)
                    me, it = ship_by_id[yielding], ship_by_id[stray]
                    route = rerouter.avoid(me, conflict_avoid_routes(me, it, a["tcpaMin"]),
                                           {"kind": "conflict", "other": stray, "since": now})
                    if not route:
                        continue
                    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
                    owner = a.get("owner", pair_owner(me.get("zone", ""), it.get("zone", ""), zmap))
                    ev = {"ts": iso, "type": "conflict_avoidance", "role": args.role,
                          "eventId": event_id("avd", iso, f"{yielding}-{stray}"),
                          "pair": [yielding, stray], "level": "danger", "cpaNm": a["cpaNm"],
                          "tcpaMin": a["tcpaMin"], "action": "rerouted", "yielding": yielding,
                          "offRoute": stray, "escapeRoute": route, "owner": owner}
                    print(f"{iso} AVOID  {yielding} 경로 변경 — 항로를 벗어난 {stray}와 "
                          f"cpa={a['cpaNm']}nm tcpa={a['tcpaMin']}min → "
                          + " → ".join(str(pt) for pt in route))
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
                    f.flush()
                    cse.publish("conflicts", ev, ["fleetAvoid", "danger"])
            except Exception as e:
                print(f"[warn] 충돌 회피 오류: {type(e).__name__}: {e} — 충돌 감시는 계속")
        active = cur
        if now - last_status >= 30.0 and closest:
            last_status = now
            print(f"  [{len(ships)}척 감시] 최소 CPA: {closest[0]} ~ {closest[1]} "
                  f"{closest[2]:.2f}nm (tcpa {closest[3]:.1f}min)")
            iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
            st = {"ts": iso, "eventId": event_id("fst", iso), "role": args.role,
                  "ships": len(ships), "state": "online",
                  "closestPair": {"a": closest[0], "b": closest[1],
                                  "cpaNm": round(closest[2], 2),
                                  "tcpaMin": round(closest[3], 1)}}
            if routes and routes.ships:
                st["routeModel"] = {"version": routes.version, "ships": len(routes.ships)}
            cse.publish("fleetStatus", st, ["fleetStatus"])
        # 정상 항적 기록(항로 학습용): 재해가 없고, 대피·복귀 중이 아니며 항로 이탈로 판정되지 않은 배만.
        if track_log and (hazards == [] or not hazard_url):
            for sh in ships:
                sid = sh["ship_id"]
                if sid in off_route or (rerouter and (sid in rerouter.moved
                                                      or now < rerouter.settling.get(sid, 0.0))):
                    continue
                track_log.write(json.dumps({"t": round(now, 1), "id": sid, "lat": sh["lat"],
                                            "lon": sh["lon"], "sog": round(sh["sog"], 2),
                                            "cog": round(sh["cog"], 1)}) + "\n")
            track_log.flush()
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

    # 재해 접근: 태풍 중심(128.9E, 34.6N) 반경 80km, 북동(30°)으로 300km/h(실시간), 20배속.
    typhoon = {"id": "typ-test", "type": "typhoon", "name": "Typhoon Test",
               "center": [128.9, 34.6], "radiusKm": 80, "headingDeg": 30, "speedKmh": 300,
               "remainingSec": 1800, "ships": ["inside-01"]}
    tsunami = {"id": "tsu-test", "type": "tsunami", "name": "Tsunami Test",
               "center": [129.6, 34.4], "quakeRadiusKm": 60, "maxRadiusKm": 250,
               "waveRadiusKm": 20, "speedKmh": 500, "remainingSec": 1800, "ships": []}
    fleet = [
        {"ship_id": "inside-01", "lat": 34.7, "lon": 129.0, "sog": 12.0, "cog": 0.0},
        {"ship_id": "ahead-02", "lat": 35.6, "lon": 129.9, "sog": 12.0, "cog": 200.0},   # 진로 앞
        {"ship_id": "behind-03", "lat": 33.6, "lon": 127.9, "sog": 12.0, "cog": 230.0},  # 뒤로 멀어짐
    ]
    got = {a["ship"]: a for a in hazard_alerts(fleet, [typhoon], 20.0, 120.0, 30.0)}
    for a in got.values():
        print(f"  태풍: {a['ship']:<10} level={a['level']:<6} eta={a['etaMin']}min "
              f"권고 침로 {a.get('escapeCourseDeg')}°")
    assert got["inside-01"]["inside"] and got["inside-01"]["level"] == "danger"
    assert "ahead-02" in got and got["ahead-02"]["etaMin"] > 0, "진로 앞의 배가 경고되지 않음"
    assert "behind-03" not in got, "멀어지는 배가 경고됨"
    wave = {a["ship"]: a for a in hazard_alerts(
        [{"ship_id": "near-04", "lat": 34.7, "lon": 129.8, "sog": 10.0, "cog": 90.0},
         {"ship_id": "far-05", "lat": 31.0, "lon": 127.0, "sog": 10.0, "cog": 180.0}],
        [tsunami], 20.0, 120.0, 30.0)}
    for a in wave.values():
        print(f"  해일: {a['ship']:<10} level={a['level']:<6} eta={a['etaMin']}min")
    assert "near-04" in wave and "far-05" not in wave, "지진해일 도달 판정 오류"
    print("셀프테스트 통과: 재해 구역 안(danger) · 진입 예정(eta) · 무관한 배 제외")

    # 대피 경로: 두 점 모두 태풍 진로에서 반경 + 30km 떨어져 있고, 첫 후보는 배와 같은 쪽.
    hd = math.radians(typhoon["headingDeg"])
    kx = 111.32 * math.cos(math.radians(typhoon["center"][1]))

    def lateral_km(lon, lat):
        px, py = (lon - typhoon["center"][0]) * kx, (lat - typhoon["center"][1]) * 110.57
        return px * math.cos(hd) - py * math.sin(hd)

    routes = typhoon_escape_routes(fleet[1], typhoon)
    ship_side = lateral_km(fleet[1]["lon"], fleet[1]["lat"])
    for lon, lat in routes[0]:
        d = lateral_km(lon, lat)
        assert abs(d) >= 99.0 and d * ship_side > 0, f"대피 지점 오류 {d:.1f}km"
    assert all(lateral_km(*pt) * ship_side < 0 for pt in routes[-1]), "반대쪽 후보 오류"
    # 충돌 회피: 동쪽으로 가는 배 앞을 북쪽으로 가로지르는 상대 → 1순위는 상대 뒤(남쪽)로 비킴
    own = {"ship_id": "o", "lat": 34.5, "lon": 129.5, "sog": 12.0, "cog": 90.0}
    oth = {"ship_id": "x", "lat": 34.46, "lon": 129.55, "sog": 12.0, "cog": 0.0}
    c = conflict_avoid_routes(own, oth, 5.0)
    meet_lon = 129.5 + (12.0 * 1.852 * 5.0 / 60.0) / (111.32 * math.cos(math.radians(34.5)))
    assert abs(c[0][0][0] - meet_lon) < 0.002 and c[0][0][1] < 34.5 - 0.03, f"상대 뒤로 비키지 않음 {c[0]}"
    assert c[0][1][0] > c[0][0][0], "비킨 뒤 자기 침로로 통과하지 않음"
    par = conflict_avoid_routes(own, {**oth, "cog": 270.0}, 5.0)       # 마주 옴 → 오른쪽(남쪽)으로
    assert par[0][0][1] < 34.5 and len(par) == 6, f"마주 올 때 오른쪽으로 비키지 않음 {par[0]}"
    print(f"셀프테스트 통과: 충돌 회피 후보 {len(c)}개, 엇갈림은 상대 뒤로·마주 오면 오른쪽으로")

    # 충돌 회피 판단: 엄격 기준(0.2nm·8분) 2회 연속 + 한 척만 항로 이탈일 때만 고른다
    def alert(cpa, tcpa, level="danger"):
        return {"pair": ["norm", "rogue"], "level": level, "cpaNm": cpa, "tcpaMin": tcpa}

    runs: dict = {}
    assert avoidance_picks([alert(0.1, 5)], {"rogue": "danger"}, runs, 0.2, 8.0) == [], "1회만에 회피"
    got = avoidance_picks([alert(0.1, 4)], {"rogue": "danger"}, runs, 0.2, 8.0)
    assert [g[:2] for g in got] == [("norm", "rogue")], f"이탈 배 상대로 회피 안 함 {got}"
    for case, off, al in (("둘 다 평소 항로", {}, alert(0.1, 4)),
                          ("둘 다 이탈", {"rogue": "warn", "norm": "warn"}, alert(0.1, 4)),
                          ("최근접 0.25nm(기준 밖)", {"rogue": "danger"}, alert(0.25, 4)),
                          ("9분 뒤(기준 밖)", {"rogue": "danger"}, alert(0.1, 9)),
                          ("warn 등급", {"rogue": "danger"}, alert(0.1, 4, "warn"))):
        r2: dict = {}
        avoidance_picks([al], off, r2, 0.2, 8.0)
        assert avoidance_picks([al], off, r2, 0.2, 8.0) == [], f"회피하면 안 되는 경우에 회피: {case}"
    assert avoidance_picks([], {}, runs, 0.2, 8.0) == [] and runs == {}, "사라진 쌍의 횟수가 남음"
    print("셀프테스트 통과: 충돌 회피는 엄격 기준 2회 연속 + 상대만 항로 이탈일 때만 (그 외 5가지 경우 무동작)")

    # 경로 변경·복귀 흐름: 지도 서버를 흉내 낸 객체로 실제 Rerouter 코드를 돌린다
    import tempfile

    class FakeResp:
        def __init__(self, code, body):
            self.status_code, self._body, self.text = code, body, json.dumps(body)

        def json(self):
            return self._body

    class FakeMap:
        def __init__(self):
            self.posts, self.land, self.loc = [], {(129.9, 34.9)}, [129.2, 34.8]
            self.path = [[129.0, 35.0], [129.5, 34.5], [130.0, 34.0]]

        def get(self, url, params=None, timeout=None):
            if url.endswith("/api/land"):
                return FakeResp(200, {"land": (params["lon"], params["lat"]) in self.land})
            return FakeResp(200, {"ues": [{"assetName": "own", "eopMode": "REVERSE", "velocity": 300,
                                           "location": {"coordinates": self.loc},
                                           "path": {"coordinates": self.path}}]})

        def post(self, url, json=None, timeout=None):
            self.posts.append(json)
            return FakeResp(200, {})

    with tempfile.TemporaryDirectory() as td:
        rr = Rerouter("http://fake")
        rr.http, rr.moved, rr.changed, rr.settling = FakeMap(), {}, {}, {}
        rr.state, rr.changed_file, rr.backup = (Path(td) / n for n in ("m.json", "c.json", "b.jsonl"))
        me = {"ship_id": "own", "lat": 34.8, "lon": 129.2, "sog": 12.0, "cog": 135.0}
        got = rr.avoid(me, [[[129.9, 34.9], [130.0, 34.9]], [[129.4, 34.9], [129.6, 34.9]]], {"hazardId": "h1"})
        assert got == [[129.4, 34.9], [129.6, 34.9]], f"육지 후보를 건너뛰지 않음 {got}"
        rr.restore_finished({"h1"}, set())
        assert "own" in rr.moved and rr.moved["own"].get("returning") is None, "태풍이 아직 있는데 복귀함"
        rr.restore_finished(set(), set())
        back = rr.http.posts[-1]
        assert rr.moved["own"]["returning"] == 2 and back["path"][-1] == [130.0, 34.0], \
            f"가던 방향의 끝점으로 안 보냄 {back}"
        rr.restore_finished(set(), set())
        assert "own" in rr.moved, "끝점에 닿기 전에 원래 경로를 걸어 버림"
        rr.http.loc = [129.9995, 34.0005]                     # 끝점 도착
        rr.restore_finished(set(), set())
        last = rr.http.posts[-1]
        assert "own" not in rr.moved and last["path"] == rr.http.path[::-1], "도착 후 원래 경로로 안 돌아감"
        assert last["eopMode"] == "REVERSE" and last["velocity"] == 300
        rr.settling.clear()
        rr.http.loc = [129.2, 34.8]
        rr.avoid(me, conflict_avoid_routes(me, {"lat": 34.7, "lon": 129.3, "cog": 0.0}, 3.0),
                 {"kind": "conflict", "other": "rogue", "since": 0.0})
        rr.restore_finished(set(), set())
        assert rr.moved["own"]["kind"] == "conflict" and rr.moved["own"].get("returning") is None, \
            "충돌 회피 중인데 재해 목록만 보고 복귀함"
        rr.restore_finished(set(), {"own"})
        assert rr.moved["own"]["returning"] is not None
        rr.avoid(me, [[[129.4, 34.9]]], {"hazardId": "h2"})   # 복귀 중 태풍이 오면 복귀를 멈추고 다시 피한다
        assert rr.moved["own"].get("returning") is None and rr.moved["own"]["hazardId"] == "h2"
        rr.restore_finished(set(), set())
        rr.http.loc = [129.9995, 34.0005]
        rr.restore_finished(set(), set())
        assert "own" not in rr.moved and "own" not in rr.changed, "깨끗한 복귀인데 판정 보류가 걸림"
        rr.avoid(me, [[[129.4, 34.9]]], {"hazardId": "h3"})
        rr.http.loc = [129.4, 34.9]
        rr.restore_finished(set(), set())
        rr.moved["own"]["return_since"] = 0.0                 # 끝점에 못 닿고 시간 초과 → 그 자리에서 복귀
        rr.restore_finished(set(), set())
        assert "own" not in rr.moved and "own" in rr.changed and "own" in rr.settling
    print("셀프테스트 통과: 경로 변경 → 육지 후보 건너뜀 → 종료 뒤 끝점까지 돌아가서 원래 경로·속력 재설정")
    print(f"셀프테스트 통과: 대피 경로 후보 {len(routes)}개, 1순위 {routes[0]} (진로에서 100km 이상, 배와 같은 쪽)")


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
    ap.add_argument("--hazard-url", default=None,
                    help="재해 목록 API (예: http://100.96.155.4:30793/api/hazards) — "
                         "주면 재해 접근 경고도 발행")
    ap.add_argument("--map-url", default=None,
                    help="지도 서버 (예: http://100.96.155.4:30793) — 재해 목록과 경로 변경 API")
    ap.add_argument("--track-log", default=None,
                    help="정상 항적을 이 파일(JSONL)에 기록 — 항로 학습 데이터. 재해 중·대피 중·복귀 "
                         "직후의 배는 남기지 않는다")
    ap.add_argument("--avoid-cpa", type=float, default=0.2,
                    help="충돌 회피(경로 변경) 기준 최근접 거리 nm — danger(0.25)보다 엄격")
    ap.add_argument("--avoid-tcpa", type=float, default=8.0,
                    help="충돌 회피 기준 최근접까지 시간(분) — danger(10)보다 엄격")
    ap.add_argument("--route-models", default=None,
                    help="학습된 항로 모델 디렉터리 (fleet_route_model.py train 결과) — 주면 항로 이탈 "
                         "경고도 발행. 모델 파일이 바뀌면 재시작 없이 다시 읽는다")
    ap.add_argument("--reroute", action="store_true",
                    help="태풍 구역에 들었거나 들어갈 배의 경로를 진로 밖으로 실제 변경하고, "
                         "재해가 끝나면 원래 경로로 복귀 (--map-url 필요)")
    ap.add_argument("--hazard-warn-min", type=float, default=120.0,
                    help="재해 진입 몇 분(시나리오 시간) 전부터 warn")
    ap.add_argument("--hazard-danger-min", type=float, default=30.0)
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
