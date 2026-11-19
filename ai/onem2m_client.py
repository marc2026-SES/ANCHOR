#!/usr/bin/env python3
"""oneM2M(ACME CSE) HTTP 클라이언트.

CLAUDE.md 규칙: 프로젝트의 모든 oneM2M 호출은 이 파일에만 둔다.
- 헤더: X-M2M-Origin, X-M2M-RI, X-M2M-RVI
- Content-Type: application/json;ty=<리소스타입>
- ACME: AE originator는 'C'/'S'로 시작, 기본 주소/CSE-ID/cseRN은 config.yaml에서 읽는다.

리소스 타입: ACP=1, AE=2, CNT=3, CIN=4, GRP=9, SUB=23, FCNT=28
"""
from __future__ import annotations

import json
import uuid

import requests

TY_ACP, TY_AE, TY_CNT, TY_CIN, TY_GRP, TY_SUB, TY_FCNT = 1, 2, 3, 4, 9, 23, 28

# oneM2M accessControlOperations 비트
ACOP_CREATE, ACOP_RETRIEVE, ACOP_UPDATE, ACOP_DELETE, ACOP_NOTIFY, ACOP_DISCOVERY = 1, 2, 4, 8, 16, 32
ACOP_ALL = 63


class OneM2MError(RuntimeError):
    def __init__(self, status: int, rsc: int, message: str):
        super().__init__(f"HTTP {status} / RSC {rsc}: {message}")
        self.status = status
        self.rsc = rsc


class OneM2MClient:
    """ACME CSE용 얇은 클라이언트. 경로는 cseRN 기준 구조적 경로('SHIP-01/ENGINE/gas')."""

    def __init__(self, cse_url: str, cse_rn: str, originator: str,
                 rvi: str = "4", timeout: float = 5.0, ca: str | bool = True):
        self.base = cse_url.rstrip("/")
        self.cse_rn = cse_rn
        self.originator = originator
        self.rvi = str(rvi)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = ca    # HTTPS CSE: 팀 CA 인증서 경로 (검증 비활성화는 금지)

    # ---------------------------------------------------------------- 저수준

    def _headers(self, ty: int | None, originator: str | None) -> dict:
        h = {
            "X-M2M-Origin": originator or self.originator,
            "X-M2M-RI": uuid.uuid4().hex[:12],
            "X-M2M-RVI": self.rvi,
            "Accept": "application/json",
        }
        h["Content-Type"] = f"application/json;ty={ty}" if ty else "application/json"
        return h

    def request(self, method: str, path: str, ty: int | None = None,
                body: dict | None = None, originator: str | None = None):
        """반환: (http_status, rsc, 응답 dict|None). path는 cseRN 포함 여부 무관."""
        p = path if path.startswith(self.cse_rn) or path.startswith("/") else f"{self.cse_rn}/{path}"
        url = f"{self.base}/{p.lstrip('/')}" if not path.startswith("/") else f"{self.base}{path}"
        resp = self.session.request(
            method, url, headers=self._headers(ty, originator),
            data=json.dumps(body) if body is not None else None, timeout=self.timeout)
        rsc = int(resp.headers.get("X-M2M-RSC", 0) or 0)
        try:
            payload = resp.json() if resp.text else None
        except ValueError:
            payload = None
        return resp.status_code, rsc, payload

    def retrieve(self, path: str, originator: str | None = None) -> dict | None:
        """RETRIEVE. 404(리소스 없음)면 None, 그 외 실패는 예외."""
        status, rsc, payload = self.request("GET", path, originator=originator)
        if status == 200:
            return payload
        if status == 404:
            return None
        raise OneM2MError(status, rsc, f"RETRIEVE {path}: {payload}")

    def discover_cins(self, cnt_path: str, lim: int = 20000,
                      originator: str | None = None) -> list[str]:
        """컨테이너 아래 CIN 구조적 경로 전체(fu=1 디스커버리, 생성순)."""
        res = self.retrieve(f"{cnt_path}?fu=1&ty={TY_CIN}&lim={int(lim)}",
                            originator=originator)
        return list((res or {}).get("m2m:uril") or [])

    def create(self, parent_path: str, ty: int, body: dict,
               originator: str | None = None) -> dict:
        status, rsc, payload = self.request("POST", parent_path, ty=ty, body=body,
                                            originator=originator)
        if status in (200, 201):
            return payload or {}
        raise OneM2MError(status, rsc, f"CREATE ty={ty} under {parent_path}: {payload}")

    def update(self, path: str, body: dict, originator: str | None = None) -> dict:
        status, rsc, payload = self.request("PUT", path, body=body, originator=originator)
        if status == 200:
            return payload or {}
        raise OneM2MError(status, rsc, f"UPDATE {path}: {payload}")

    def delete(self, path: str, originator: str | None = None) -> bool:
        status, rsc, payload = self.request("DELETE", path, originator=originator)
        if status in (200, 202):
            return True
        if status == 404:
            return False
        raise OneM2MError(status, rsc, f"DELETE {path}: {payload}")

    def _ensure(self, parent_path: str, rn: str, ty: int, body: dict,
                originator: str | None = None) -> tuple[dict, bool]:
        """있으면 재사용, 없으면 생성. 반환 (리소스, created)."""
        existing = self.retrieve(f"{parent_path}/{rn}", originator=originator)
        if existing is not None:
            return existing, False
        try:
            return self.create(parent_path, ty, body, originator=originator), True
        except OneM2MError as e:
            if e.rsc == 4105 or e.status == 409:  # conflict: 경쟁 생성 → 재조회
                got = self.retrieve(f"{parent_path}/{rn}", originator=originator)
                if got is not None:
                    return got, False
            raise

    # ---------------------------------------------------------------- 리소스별

    def ensure_ae(self, rn: str, api: str, poa: list[str] | None = None,
                  rr: bool = True) -> tuple[dict, bool]:
        """AE 등록(이미 있으면 재사용). originator는 이 클라이언트의 originator('C…')."""
        ae: dict = {"rn": rn, "api": api, "rr": bool(poa) and rr, "srv": [self.rvi]}
        if poa:
            ae["poa"] = poa
        return self._ensure(self.cse_rn, rn, TY_AE, {"m2m:ae": ae})

    def ensure_container(self, parent_path: str, rn: str, mni: int | None = None,
                         acpi: list[str] | None = None,
                         originator: str | None = None) -> tuple[dict, bool]:
        cnt: dict = {"rn": rn}
        if mni is not None:
            cnt["mni"] = mni
        if acpi:
            cnt["acpi"] = acpi
        return self._ensure(parent_path, rn, TY_CNT, {"m2m:cnt": cnt}, originator=originator)

    def ensure_flexcontainer(self, parent_path: str, rn: str, key: str, cnd: str,
                             attrs: dict) -> tuple[dict, bool]:
        """flexContainer 생성/재사용. key는 '네임스페이스:이름'(예: anchor:anm) —
        ACME에 해당 specialization 정책(acme/anchor-anomaly.fcp)이 등록돼 있어야 한다."""
        body = {key: {"rn": rn, "cnd": cnd, **attrs}}
        return self._ensure(parent_path, rn, TY_FCNT, body)

    def update_flexcontainer(self, path: str, key: str, attrs: dict) -> dict:
        return self.update(path, {key: attrs})

    def create_cin(self, parent_path: str, con, lbl: list[str] | None = None,
                   originator: str | None = None) -> dict:
        cin: dict = {"cnf": "application/json:0",
                     "con": con if isinstance(con, str) else json.dumps(con, ensure_ascii=False)}
        if lbl:
            cin["lbl"] = lbl
        return self.create(parent_path, TY_CIN, {"m2m:cin": cin}, originator=originator)

    def ensure_subscription(self, target_path: str, rn: str, nu: list[str],
                            net: tuple[int, ...] = (3,), nct: int = 1) -> tuple[dict, bool]:
        """구독 생성/재사용. net=(3,)=자식 리소스 생성 시 notify, nct=1=모든 속성 포함."""
        body = {"m2m:sub": {"rn": rn, "nu": nu, "enc": {"net": list(net)}, "nct": nct}}
        return self._ensure(target_path, rn, TY_SUB, body)

    def ensure_group(self, parent_path: str, rn: str, mids: list[str],
                     mt: int = 3) -> tuple[dict, bool]:
        """group 생성/재사용. mids는 멤버 리소스 ID 목록, mt=3(container).

        <group>/fopt 에 요청하면 모든 멤버로 fanOut 된다 (액추에이터 일괄 제어)."""
        body = {"m2m:grp": {"rn": rn, "mt": mt, "mnm": max(len(mids), 1), "mid": mids}}
        return self._ensure(parent_path, rn, TY_GRP, body)

    def fanout_create_cin(self, group_path: str, con, lbl: list[str] | None = None) -> dict:
        """group fanOut: <group>/fopt 아래 CIN CREATE → 모든 멤버 컨테이너에 생성."""
        return self.create_cin(f"{group_path}/fopt", con, lbl=lbl)

    def ensure_acp(self, parent_path: str, rn: str, grants: list[tuple[list[str], int]],
                   self_originators: list[str], originator: str | None = None) -> tuple[dict, bool]:
        """ACP 생성/재사용. grants = [(originator 목록, acop 비트)], self_originators = ACP 수정 권한."""
        body = {"m2m:acp": {
            "rn": rn,
            "pv": {"acr": [{"acor": acor, "acop": acop} for acor, acop in grants]},
            "pvs": {"acr": [{"acor": self_originators, "acop": ACOP_ALL}]},
        }}
        return self._ensure(parent_path, rn, TY_ACP, body, originator=originator)

    # ---------------------------------------------------------------- 유틸

    @staticmethod
    def resource_attr(resource: dict, attr: str):
        """{'m2m:ae': {...}} 형태 응답에서 속성 꺼내기 (키 이름 무관)."""
        for v in resource.values():
            if isinstance(v, dict) and attr in v:
                return v[attr]
        return None
