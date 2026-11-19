#!/usr/bin/env python3
"""ANCHOR AI 분석 AE (ACME CSE 연동).

- 시작 시 MN-CSE에 AE 등록, anomaly flexContainer와 summary 컨테이너 생성(이미 있으면 재사용)
- config.yaml의 센서 컨테이너마다 subscription을 만들고 로컬 HTTP 서버로 notification 수신
- 30s 윈도우(5s stride)가 차면 Isolation Forest 추론 → anomaly flexContainer UPDATE
  (anomaly.skip_normal=true면 level이 normal일 때 기록 생략)
  + 같은 내용을 anomaly/history 컨테이너 CIN으로도 기록 — 결정적 eventId로
    oneM2M 파트의 누락 복구(eventId·본문 대조)가 집계·재전송할 수 있게 한다
- 1분(summary.period_s)마다 센서별 mean/max/min summary CIN 생성
- CSE 주소·AE 이름·센서 경로 등은 전부 config.yaml에서 읽는다 (oneM2M 호출은 onem2m_client.py)

테스트 (CSE 불필요):
  python analytics_ae.py --replay data/eval
  → eval.csv 행을 notification처럼 시간순으로 흘려 같은 파이프라인을 태우고,
    결과를 results/replay_anomalies.jsonl / results/replay_summary.jsonl 에 기록

라이브:
  python analytics_ae.py [--config config.yaml]
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import tarfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import yaml

import features
from features import (FFT_SENSORS, SRC_CONTAINERS, STRIDE_S, SUBSYSTEM_SENSORS,
                      WINDOW_S, _stats, load_csv)
from link_forecast import LinkForecaster, event_id
from model_iforest import TS_FMT, load_models, score_array
from prognosis import TrendPrognoser
from onem2m_client import ACOP_ALL, OneM2MClient, OneM2MError

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"

HATCH = ("CARGO", "hatch")
SENSOR_KEYS: dict[tuple[str, str], list[tuple[str, int]]] = {}


def _build_sensor_keys() -> None:
    """features.set_profile() 후 재호출해 센서 키 맵을 재구성한다."""
    SENSOR_KEYS.clear()
    for _sub, _sensors in SUBSYSTEM_SENSORS.items():
        for _cont, _key, _step in _sensors:
            SENSOR_KEYS.setdefault((_sub, _cont), []).append((_key, _step))


_build_sensor_keys()

INFER_GUARD_S = 1.0  # 센서 간 도착 시차 허용 (데이터 시계 기준)
LEVEL_RANK = {"normal": 0, "warn": 1, "critical": 2}


def iso(ep: float) -> str:
    return datetime.fromtimestamp(ep, tz=timezone.utc).strftime(TS_FMT)


def parse_ts(s: str) -> float:
    return datetime.strptime(s, TS_FMT).replace(tzinfo=timezone.utc).timestamp()


def ct_epoch(ct) -> float | None:
    """ACME ct('20261118T061223,912922') → epoch 초."""
    if not isinstance(ct, str) or "T" not in ct:
        return None
    base, _, frac = ct.partition(",")
    try:
        dt = datetime.strptime(base, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return dt.timestamp() + (float("0." + frac) if frac else 0.0)


def ts_sort_key(ts) -> float:
    """con의 ts(epoch ms 숫자 또는 ISO 문자열)를 정렬용 epoch 초로 — 실패 시 0."""
    if isinstance(ts, (int, float)):
        return float(ts) / 1000.0 if ts > 1e11 else float(ts)
    if isinstance(ts, str):
        try:
            return parse_ts(ts)
        except ValueError:
            return 0.0
    return 0.0


# ================================================================ 파이프라인

class WindowPipeline:
    """스트리밍 샘플 → 윈도우 특징 → IF 점수 + 1분 summary. 전부 데이터 시각 기준."""

    def __init__(self, bundles: dict, on_result, on_summary, summary_s: int = 60):
        self.bundles = bundles
        self.on_result = on_result
        self.on_summary = on_summary
        self.summary_s = int(summary_s)
        self.lock = threading.Lock()

        self.buf: dict[tuple[str, str, str], list[tuple[float, float]]] = {}
        self.hatch_events: list[tuple[float, float]] = []
        self.hatch_state0 = 0.0          # 보존된 이전 상태 (초기 closed 가정)
        self.next_end: dict[str, float | None] = {s: None for s in bundles}
        self.first: dict[str, float | None] = {s: None for s in bundles}
        self.latest: dict[str, float] = {s: 0.0 for s in bundles}
        self.windows = {s: 0 for s in bundles}
        self.skipped = {s: 0 for s in bundles}

        self.sum_start: float | None = None
        self.sum_acc: dict[str, list[float]] = {}   # "SUB/cont.key" -> [n, sum, min, max]

    # ---- 입력 ----

    def add(self, subsystem: str, container: str, values: dict, ts: datetime) -> None:
        ep = ts.timestamp()
        with self.lock:
            if (subsystem, container) == HATCH:
                st = 1.0 if str(values.get("state")) == "open" else 0.0
                self.hatch_events.append((ep, st))
            else:
                for key, _step in SENSOR_KEYS.get((subsystem, container), []):
                    if key not in values:
                        continue
                    v = float(values[key])
                    self.buf.setdefault((subsystem, container, key), []).append((ep, v))
                    self._acc_summary(f"{subsystem}/{container}.{key}", v, ep)
            if subsystem in self.bundles:
                if self.first[subsystem] is None:
                    self.first[subsystem] = ep
                self.latest[subsystem] = max(self.latest[subsystem], ep)
                self._infer(subsystem, guard=INFER_GUARD_S)

    def flush(self) -> None:
        """스트림 종료 시 남은 윈도우·summary 강제 처리 (replay 용)."""
        with self.lock:
            for sub in self.bundles:
                self._infer(sub, guard=0.0)
            self._flush_summary()

    # ---- 윈도우 추론 ----

    def _infer(self, sub: str, guard: float) -> None:
        if self.first[sub] is None:
            return
        if self.next_end[sub] is None:
            start0 = float(np.ceil(self.first[sub] / STRIDE_S) * STRIDE_S)
            self.next_end[sub] = start0 + WINDOW_S
        while self.next_end[sub] + guard <= self.latest[sub] + 1e-9:
            end = self.next_end[sub]
            x = self._vector(sub, end)
            if x is not None:
                s01, level, top = score_array(self.bundles[sub], x)
                self.windows[sub] += 1
                self.on_result({
                    "eventId": event_id("anm", iso(end), sub),
                    "subsystem": sub,
                    "level": str(level[0]),
                    "score": round(float(s01[0]), 4),
                    "topFeatures": top[0],
                    "windowStart": iso(end - WINDOW_S),
                    "windowEnd": iso(end),
                    "srcContainers": SRC_CONTAINERS[sub],
                })
            else:
                self.skipped[sub] += 1
            self.next_end[sub] = end + STRIDE_S
            self._prune(sub, self.next_end[sub] - WINDOW_S)

    def _vector(self, sub: str, end: float) -> np.ndarray | None:
        feats: dict[str, float] = {}
        a = end - WINDOW_S
        for cont, key, step in SUBSYSTEM_SENSORS[sub]:
            samples = [(t, v) for t, v in self.buf.get((sub, cont, key), ())
                       if a - 1e-9 <= t < end - 1e-9]
            need = WINDOW_S // step
            if len(samples) != need:
                # 실센서(ship 프로필)는 주기가 흔들림 — 충분히 찼으면 격자 보간으로 맞춘다
                if not features.TOLERANT_WINDOWS or len(samples) < max(4, int(0.7 * need)):
                    return None                         # 윈도우 미완성
                samples.sort()
                st = np.array([t for t, _ in samples])
                sv = np.array([v for _, v in samples])
                grid = a + step * np.arange(need, dtype=float)
                m = np.interp(grid, st, sv)[None, :]
                trel = (grid - a)[None, :]
            else:
                m = np.array([v for _, v in samples])[None, :]
                trel = np.array([t - a for t, _ in samples])[None, :]
            st = _stats(m, trel, f"{cont}.{key}", (cont, key) in FFT_SENSORS)
            feats.update({k: float(v[0]) for k, v in st.items()})
        if sub == "CARGO":
            feats["hatch.open_frac"] = self._open_frac(a, end)
        names = self.bundles[sub]["feature_names"]
        return np.array([[feats[n] for n in names]])

    def _open_frac(self, a: float, b: float) -> float:
        cur = self.hatch_state0
        pts: list[tuple[float, float]] = []
        for t, st in self.hatch_events:
            if t <= a:
                cur = st
            elif t < b:
                pts.append((t, st))
        open_sec, last_t, last_s = 0.0, a, cur
        for t, st in pts:
            open_sec += last_s * (t - last_t)
            last_t, last_s = t, st
        open_sec += last_s * (b - last_t)
        return open_sec / (b - a)

    def _prune(self, sub: str, keep_from: float) -> None:
        for cont, key, _step in SUBSYSTEM_SENSORS[sub]:
            k = (sub, cont, key)
            if k in self.buf:
                self.buf[k] = [(t, v) for t, v in self.buf[k] if t >= keep_from - 1e-9]
        if sub == "CARGO":
            kept: list[tuple[float, float]] = []
            for t, st in self.hatch_events:
                if t < keep_from:
                    self.hatch_state0 = st
                else:
                    kept.append((t, st))
            self.hatch_events = kept

    # ---- summary ----

    def _acc_summary(self, name: str, v: float, ep: float) -> None:
        if self.sum_start is None:
            self.sum_start = ep // self.summary_s * self.summary_s
        while ep >= self.sum_start + self.summary_s:
            self._flush_summary()
            self.sum_start += self.summary_s
            if ep - self.sum_start > 10 * self.summary_s:   # 큰 공백은 건너뜀
                self.sum_start = ep // self.summary_s * self.summary_s
        acc = self.sum_acc.setdefault(name, [0, 0.0, v, v])
        acc[0] += 1
        acc[1] += v
        acc[2] = min(acc[2], v)
        acc[3] = max(acc[3], v)

    def _flush_summary(self) -> None:
        if not self.sum_acc or self.sum_start is None:
            return
        stats = {name: {"mean": round(s / n, 3), "min": round(lo, 3),
                        "max": round(hi, 3), "n": n}
                 for name, (n, s, lo, hi) in sorted(self.sum_acc.items())}
        self.on_summary({
            "eventId": event_id("sum", iso(self.sum_start + self.summary_s)),
            "windowStart": iso(self.sum_start),
            "windowEnd": iso(self.sum_start + self.summary_s),
            "stats": stats,
        })
        self.sum_acc = {}


# ================================================================ 라이브 모드

class NotificationServer(ThreadingHTTPServer):
    daemon_threads = True
    on_sgn = staticmethod(lambda sgn: None)
    notify_path = "/notify"


class NotificationHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except ValueError:
            body = {}
        sgn = body.get("m2m:sgn") or {}
        if not sgn.get("vrq"):
            try:
                self.server.on_sgn(sgn)
            except Exception as e:                     # 수신은 항상 ACK
                print(f"[warn] notification 처리 실패: {e}")
        self.send_response(200)
        self.send_header("X-M2M-RSC", "2000")
        if self.headers.get("X-M2M-RI"):
            self.send_header("X-M2M-RI", self.headers["X-M2M-RI"])
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args):  # 기본 액세스 로그 끄기
        pass


class LiveApp:
    def __init__(self, cfg: dict, bundles: dict):
        self.cfg = cfg
        self.o = cfg["onem2m"]
        self.client = OneM2MClient(self.o["cse_url"], self.o["cse_rn"],
                                   self.o["ae"]["originator"], rvi=str(self.o.get("rvi", "4")),
                                   ca=self.o.get("ca_cert", True))
        self.skip_normal = bool(self.o["anomaly"].get("skip_normal", True))
        self.fcnt_key = self.o["anomaly"].get("key", "anchor:anm")
        self.use_fcnt = True
        self.pending_subs: list[str] = []
        self.sur_table: list[tuple[str, tuple[str, str]]] = []  # (식별자, (subsystem, container))
        self.pipeline = WindowPipeline(bundles, self._on_result, self._on_summary,
                                       summary_s=int(self.o["summary"].get("period_s", 60)))
        n = self.o["notification"]
        self.poa_url = f"http://{n['host']}:{n['port']}{n.get('path', '/notify')}"
        self.ae_path = f"{self.o['cse_rn']}/{self.o['ae']['rn']}"
        self.anomaly_path = f"{self.ae_path}/{self.o['anomaly']['rn']}"
        self.history_rn = str(self.o["anomaly"].get("history_rn", "history"))
        self.history_path = f"{self.anomaly_path}/{self.history_rn}"
        self.history_ready = False
        self.summary_path = f"{self.ae_path}/{self.o['summary']['rn']}"

        lf = cfg.get("link_forecast") or {}                     # 링크 예측 (최상단 섹션)
        self.forecaster = LinkForecaster(lf, self._on_link) if lf.get("enabled") else None
        link_rn = (self.o.get("link_forecast") or {}).get("container_rn", "linkForecast")
        self.link_path = f"{self.ae_path}/{link_rn}"

        pg = cfg.get("prognosis") or {}                         # 고장 예지 (최상단 섹션)
        self.prognoser = (TrendPrognoser(pg, cfg.get("thresholds") or {}, self._on_prog)
                          if pg.get("enabled") else None)
        prog_rn = (self.o.get("prognosis") or {}).get("container_rn", "prognosis")
        self.prog_path = f"{self.ae_path}/{prog_rn}"

        act = self.o.get("actuators") or {}                     # critical 시 group fanOut
        self.act_members = [m.strip("/") for m in act.get("members", [])]
        self.act_on_level = str(act.get("on_level", "critical"))
        self.group_rn = str(act.get("group_rn", "grpAlarm"))
        self.group_path = f"{self.ae_path}/{self.group_rn}"
        self.group_ready = False
        self.siren_on = False
        self.levels: dict[str, str] = {}

        po = self.o.get("poll") or {}                           # 폴링 수집 (구독 대체)
        self.poll_enabled = bool(po.get("enabled"))
        self.poll_interval = float(po.get("interval_s", 5.0))
        self.poll_read_origin = po.get("read_originator")       # 센서 읽기 originator(임시 CAdmin 등)
        self._poll_seen: dict[str, set[str]] = {}
        stc = self.o.get("status") or {}                        # 대시보드용 상태 하트비트
        self.status_rn = stc.get("rn")
        self.status_period = float(stc.get("period_s", 30))
        self.status_path = f"{self.ae_path}/{self.status_rn}" if self.status_rn else None

        self.readers = [str(r) for r in (self.o.get("readers") or [])]  # 조회자(acop 34)
        mir = self.o.get("mirror") or {}                        # 결과 이중 기록 (육상 등)
        self.mirror_client = (OneM2MClient(mir["cse_url"], mir.get("cse_rn", "cse-in"),
                                           self.o["ae"]["originator"],
                                           rvi=str(mir.get("rvi", self.o.get("rvi", "3"))),
                                           ca=mir.get("ca_cert", True))
                              if mir.get("cse_url") else None)
        self.mirror_ready = False
        self._mirror_warn_at = 0.0

        mr = self.o.get("model_registry") or {}                 # 모델 배포 루프 (핫스왑)
        self.registry_poll_s = float(mr.get("poll_s", 30))
        self.registry_rn = str(mr.get("container_rn", "modelRegistry"))
        self.registry_client = None
        if mr.get("enabled"):
            self.registry_client = (OneM2MClient(mr["cse_url"], mr.get("cse_rn", "cse-in"),
                                                 self.o["ae"]["originator"],
                                                 rvi=str(mr.get("rvi", self.o.get("rvi", "3"))))
                                    if mr.get("cse_url") else self.mirror_client)
        self._registry_seen = None
        self.models_dir = str((cfg.get("paths") or {}).get("models_dir") or (ROOT / "models"))
        try:
            card = json.loads((Path(self.models_dir) if Path(self.models_dir).is_absolute()
                               else ROOT / self.models_dir).joinpath("model_card.json")
                              .read_text(encoding="utf-8"))
            self.model_version = str(card.get("version") or card.get("trained_at") or "?")
        except OSError:
            self.model_version = "?"

    # ---- 리소스 셋업 ----

    def setup_resources(self) -> None:
        ae, created = self.client.ensure_ae(self.o["ae"]["rn"], self.o["ae"]["api"],
                                            poa=[self.poa_url])
        print(f"AE {self.o['ae']['rn']}: {'등록' if created else '재사용'}")

        attrs0 = {"sbs": "-", "lvl": "normal", "scr": 0.0, "tpf": "[]",
                  "wst": "-", "wen": "-", "srcc": "[]"}
        try:
            fc, created = self.client.ensure_flexcontainer(
                self.ae_path, self.o["anomaly"]["rn"], self.fcnt_key,
                self.o["anomaly"]["cnd"], attrs0)
            if "m2m:cnt" in (fc or {}):     # 과거 fallback으로 만든 일반 컨테이너가 재사용된 경우
                self.use_fcnt = False
                print("anomaly: 기존 fallback 컨테이너 재사용 (container+CIN)")
            else:
                print(f"anomaly flexContainer: {'생성' if created else '재사용'}")
            try:        # FCNT는 최신 상태만 → 이력은 하위 컨테이너 CIN (oneM2M 파트 누락 복구 대상)
                self.client.ensure_container(self.anomaly_path, self.history_rn, mni=1000)
                self.history_ready = True
                print(f"anomaly history 컨테이너: 준비 ({self.o['anomaly']['rn']}/{self.history_rn})")
            except OneM2MError as e:
                print(f"[warn] anomaly history 컨테이너 실패: {e} — 이력 CIN 생략")
        except OneM2MError as e:
            if not self.o["anomaly"].get("fallback_container", True):
                raise
            self.use_fcnt = False
            self.client.ensure_container(self.ae_path, self.o["anomaly"]["rn"], mni=1000)
            print(f"[warn] flexContainer 생성 실패({e.rsc}) — container+CIN으로 대체. "
                  f"acme/anchor-anomaly.fcp 를 ACME init 디렉터리에 복사했는지 확인하세요")
        _, created = self.client.ensure_container(self.ae_path, self.o["summary"]["rn"], mni=1440)
        print(f"summary 컨테이너: {'생성' if created else '재사용'}")
        if self.forecaster:
            self.client.ensure_container(self.ae_path, self.link_path.rsplit("/", 1)[-1], mni=500)
            print("linkForecast 컨테이너: 준비")
        if self.prognoser:
            self.client.ensure_container(self.ae_path, self.prog_path.rsplit("/", 1)[-1], mni=500)
            print("prognosis 컨테이너: 준비")

        if self.status_path:
            self.client.ensure_container(self.ae_path, self.status_rn, mni=100)
            print(f"aiStatus 컨테이너: 준비 ({self.status_rn})")
        self._apply_read_acp(self.client)
        if self.mirror_client:
            self._setup_mirror()
        if self.registry_client:
            try:
                self.registry_client.ensure_container(self.o["ae"]["rn"], self.registry_rn,
                                                      mni=20)
                print(f"modelRegistry 감시: {self.registry_client.base} "
                      f"({self.registry_rn}, {self.registry_poll_s:g}s 주기)")
            except (OneM2MError, OSError) as e:
                print(f"[warn] modelRegistry 준비 실패: {e} — 핫스왑 비활성")
                self.registry_client = None
        if self.poll_enabled:
            print("폴링 수집 모드 — 센서 구독 생략 (읽기 전용 디스커버리)")
        else:
            self.pending_subs = [p.strip("/") for p in self.o["sensors"]]
            self._try_subscriptions(verbose=True)
            if self.pending_subs:
                print(f"[warn] 구독 대기 {len(self.pending_subs)}개 — 센서 리소스가 생기면 자동 재시도")
        self._try_group()

    def _try_subscriptions(self, verbose: bool = False) -> None:
        sub_rn = f"sub-{self.o['ae']['rn']}"
        still: list[str] = []
        for path in self.pending_subs:
            subsystem, container = path.split("/")[-2].upper(), path.split("/")[-1]
            target = f"{self.o['cse_rn']}/{path}"
            try:
                sub, created = self.client.ensure_subscription(target, sub_rn, [self.poa_url])
                ri = self.client.resource_attr(sub, "ri") or ""
                self.sur_table.append((f"{path}/{sub_rn}", (subsystem, container)))
                if ri:
                    self.sur_table.append((str(ri), (subsystem, container)))
                print(f"subscription {path}: {'생성' if created else '재사용'}")
            except OneM2MError as e:
                still.append(path)
                if verbose:
                    print(f"[warn] subscription 실패 {path}: {e} — 센서 리소스/접근권한 확인")
        self.pending_subs = still

    def _try_group(self) -> None:
        """액추에이터 group 생성/재사용. 멤버 컨테이너가 아직 없으면 다음 틱에 재시도."""
        if not self.act_members or self.group_ready:
            return
        mids: list[str] = []
        for m in self.act_members:
            res = self.client.retrieve(f"{self.o['cse_rn']}/{m}")
            if res is None:
                return                      # 멤버 미생성 — feed_csv가 만들면 재시도 성공
            mids.append(str(self.client.resource_attr(res, "ri")))
        try:
            _, created = self.client.ensure_group(self.ae_path, self.group_rn, mids)
            self.group_ready = True
            print(f"actuator group {self.group_rn}: {'생성' if created else '재사용'} "
                  f"({len(mids)} members, on_level={self.act_on_level})")
        except OneM2MError as e:
            print(f"[warn] group 생성 실패: {e}")

    def _resolve_sur(self, sur: str) -> tuple[str, str] | None:
        s = (sur or "").strip("/")
        for ident, sc in self.sur_table:
            if s.endswith(ident.strip("/")):
                return sc
        return None

    # ---- notification → 파이프라인 ----

    def on_sgn(self, sgn: dict) -> None:
        nev = sgn.get("nev") or {}
        cin = (nev.get("rep") or {}).get("m2m:cin")
        if not cin:
            return
        target = self._resolve_sur(sgn.get("sur", ""))
        if target is None:
            return
        subsystem, container = target
        con = cin.get("con")
        try:
            data = json.loads(con) if isinstance(con, str) else dict(con or {})
        except ValueError:
            return
        self._ingest(subsystem, container, data)

    def _ingest(self, subsystem: str, container: str, data: dict) -> None:
        """센서 샘플 1건 처리 — 구독 notification과 폴링 수집이 공용으로 쓴다."""
        ts_s = data.pop("ts", None)
        try:
            if isinstance(ts_s, (int, float)) and ts_s > 0:     # 실센서: UTC epoch 밀리초
                ts = datetime.fromtimestamp(float(ts_s) / 1000.0, tz=timezone.utc)
            elif ts_s:
                ts = datetime.strptime(ts_s, TS_FMT).replace(tzinfo=timezone.utc)
            else:
                ts = datetime.now(timezone.utc)
        except (ValueError, OSError, OverflowError):
            ts = datetime.now(timezone.utc)
        if (subsystem, container) == ("BRIDGE", "position"):    # 링크 예측 전용 스트림
            if self.forecaster and data.get("fix", True) is not False:  # GPS 미확보 샘플 제외
                try:
                    self.forecaster.update(float(data["lat"]), float(data["lon"]),
                                           float(data.get("sog", data.get("sog_kn", 0.0))),
                                           float(data.get("cog", 0.0)),
                                           ts.timestamp(), ts.strftime(TS_FMT))
                except (KeyError, TypeError, ValueError) as e:
                    print(f"[warn] position 파싱 실패: {e}")
            return
        if self.prognoser:
            self.prognoser.observe(subsystem, container, data,
                                   ts.timestamp(), ts.strftime(TS_FMT))
        self.pipeline.add(subsystem, container, data, ts)

    # ---- 폴링 수집 (구독 권한 없는 CSE용 — 읽기 전용 디스커버리 차집합) ----

    def _poll_seed(self) -> None:
        """기존 CIN 경로를 '본 것'으로 표시 — 이후 새로 생기는 CIN만 처리."""
        for path in [p.strip("/") for p in self.o["sensors"]]:
            try:
                self._poll_seen[path] = set(self.client.discover_cins(
                    f"{self.o['cse_rn']}/{path}", originator=self.poll_read_origin))
            except (OneM2MError, OSError) as e:
                self._poll_seen[path] = set()
                print(f"[warn] 폴링 시드 실패 {path}: {e}")
        print(f"폴링 시드 완료 — 컨테이너 {len(self._poll_seen)}개, 이후 새 CIN만 처리")

    def _poll_once(self) -> int:
        batch: list[tuple[float, str, str, dict]] = []
        for path in [p.strip("/") for p in self.o["sensors"]]:
            subsystem, container = path.split("/")[-2].upper(), path.split("/")[-1]
            try:
                uril = self.client.discover_cins(f"{self.o['cse_rn']}/{path}",
                                                 originator=self.poll_read_origin)
            except (OneM2MError, OSError) as e:
                print(f"[warn] 폴링 실패 {path}: {e} — 다음 라운드 재시도")
                continue
            seen = self._poll_seen.setdefault(path, set())
            for u in uril:
                if u in seen:
                    continue
                try:
                    res = self.client.retrieve(u, originator=self.poll_read_origin)
                except (OneM2MError, OSError):
                    continue                       # seen 미추가 — 다음 라운드 재시도
                seen.add(u)
                cin = (res or {}).get("m2m:cin") or {}
                try:
                    con = cin.get("con")
                    data = json.loads(con) if isinstance(con, str) else dict(con or {})
                except ValueError:
                    continue
                ct_s = ct_epoch(cin.get("ct"))
                ts = data.get("ts")
                if ct_s and isinstance(ts, (int, float)) \
                        and abs(ts / 1000.0 - ct_s) > 3600.0:
                    data["ts"] = ct_s * 1000.0     # 장치 NTP 미동기 — CSE 수신시각으로 보정
                batch.append((ts_sort_key(data.get("ts")), subsystem, container, data))
        for _, subsystem, container, data in sorted(batch, key=lambda b: b[0]):
            self._ingest(subsystem, container, data)
        return len(batch)

    # ---- 조회 ACP · 미러(이중 기록) ----

    def _result_containers(self) -> list[tuple[str, int]]:
        """(AE 상대 rn, mni) — 산출물 컨테이너 목록."""
        out = [(self.o["anomaly"]["rn"], 1000), (self.o["summary"]["rn"], 1440),
               (self.link_path.rsplit("/", 1)[-1], 500),
               (self.prog_path.rsplit("/", 1)[-1], 500)]
        if self.status_rn:
            out.append((self.status_rn, 100))
        return out

    def _apply_read_acp(self, client: OneM2MClient) -> None:
        """대시보드 등 조회자에게 산출물 조회+디스커버리(acop 34)를 여는 ACP."""
        if not self.readers:
            return
        ae_rel = self.o["ae"]["rn"]
        me = self.o["ae"]["originator"]
        try:
            acp, _ = client.ensure_acp(ae_rel, "acpAnchorAI",
                                       grants=[([me], ACOP_ALL), (self.readers, 34)],
                                       self_originators=[me])
            ri = str(client.resource_attr(acp, "ri"))
        except OneM2MError as e:
            print(f"[warn] 조회 ACP 생성 실패: {e}")
            return
        rns = [rn for rn, _ in self._result_containers()]
        if self.use_fcnt and self.history_ready:
            rns.append(f"{self.o['anomaly']['rn']}/{self.history_rn}")
        for rn in rns:
            path = f"{ae_rel}/{rn}"
            try:
                res = client.retrieve(path)
                if res is not None and not client.resource_attr(res, "acpi"):
                    client.update(path, {"m2m:cnt": {"acpi": [ri]}})
            except OneM2MError as e:
                print(f"[warn] acpi 설정 실패 {rn}: {e}")
        print(f"조회 ACP 적용: {', '.join(self.readers)} (acop 34, {client.base})")

    def _setup_mirror(self) -> None:
        """육상 CSE에 같은 이름의 산출물 트리를 만들어 이중 기록을 준비."""
        m = self.mirror_client
        ae_rel = self.o["ae"]["rn"]
        try:
            m.ensure_ae(ae_rel, self.o["ae"]["api"])
            for rn, mni in self._result_containers():
                m.ensure_container(ae_rel, rn, mni=mni)
            self._apply_read_acp(m)
            self.mirror_ready = True
            print(f"미러 CSE 준비: {m.base}/{m.cse_rn} — 결과 이중 기록")
        except (OneM2MError, OSError) as e:
            print(f"[warn] 미러 CSE 준비 실패: {e} — 이중 기록 비활성")

    def _mirror_cin(self, rn: str, body, lbl: list | None = None) -> None:
        if not self.mirror_ready:
            return
        try:
            self.mirror_client.create_cin(f"{self.o['ae']['rn']}/{rn}", body, lbl=lbl)
        except (OneM2MError, OSError) as e:
            now = time.time()
            if now - self._mirror_warn_at > 60:                 # 경고 스팸 방지
                self._mirror_warn_at = now
                print(f"[warn] 미러 기록 실패 {rn}: {e}")

    def _check_model_update(self) -> None:
        """레지스트리의 새 모델 CIN을 감지해 해시 검증 후 무중단 교체."""
        c = self.registry_client
        if not c:
            return
        try:
            res = c.retrieve(f"{self.o['ae']['rn']}/{self.registry_rn}/la")
        except (OneM2MError, OSError):
            return
        cin = (res or {}).get("m2m:cin") or {}
        try:
            d = json.loads(cin.get("con") or "{}")
        except ValueError:
            return
        eid = d.get("eventId") or cin.get("ri")
        if not d.get("data") or eid == self._registry_seen:
            return
        self._registry_seen = eid
        ver = str(d.get("version") or "")
        if not ver or ver == self.model_version:
            return
        try:
            blob = base64.b64decode(d["data"])
            if hashlib.sha256(blob).hexdigest() != d.get("sha256"):
                print(f"[warn] 모델 {ver}: sha256 불일치 — 적용 거부")
                return
            mdir = Path(self.models_dir)
            if not mdir.is_absolute():
                mdir = ROOT / mdir
            mdir.mkdir(parents=True, exist_ok=True)
            with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
                for m in tf.getmembers():
                    name = Path(m.name).name       # 경로 평탄화 — 디렉터리 탈출 차단
                    if not m.isfile() or not (name.endswith(".joblib")
                                              or name.endswith(".json")):
                        continue
                    (mdir / name).write_bytes(tf.extractfile(m).read())
            bundles = load_models(str(mdir))
            with self.pipeline.lock:
                self.pipeline.bundles = bundles
            self.model_version = ver
            print(f"모델 교체 완료: {ver} ({d.get('profile')}, {len(blob):,}B) — 무중단 적용")
        except Exception as e:
            print(f"[warn] 모델 교체 실패({ver}): {e}")

    def _write_status(self) -> None:
        """대시보드용 AI 상태 하트비트 CIN."""
        now_iso = datetime.now(timezone.utc).strftime(TS_FMT)
        st = {"state": "online", "profile": features.ACTIVE_PROFILE, "ts": now_iso,
              "eventId": event_id("sts", now_iso),
              "windows": int(sum(self.pipeline.windows.values())),
              "levels": dict(self.levels), "sirenOn": bool(self.siren_on),
              "modelVersion": self.model_version}
        try:
            self.client.create_cin(self.status_path, st, lbl=["aiStatus"])
        except OneM2MError as e:
            print(f"[warn] aiStatus 기록 실패: {e}")
        self._mirror_cin(self.status_rn, st, lbl=["aiStatus"])

    # ---- 산출물 기록 ----

    def _on_result(self, res: dict) -> None:
        self.levels[res["subsystem"]] = res["level"]
        self._update_siren(res)
        if self.skip_normal and res["level"] == "normal":
            return
        print(f"{res['windowEnd']} {res['subsystem']:<6} {res['level']:<8} "
              f"score={res['score']:.3f} top={res['topFeatures'][0]['feature']}")
        attrs = {"sbs": res["subsystem"], "lvl": res["level"], "scr": res["score"],
                 "tpf": json.dumps(res["topFeatures"], ensure_ascii=False),
                 "wst": res["windowStart"], "wen": res["windowEnd"],
                 "srcc": json.dumps(res["srcContainers"])}
        try:
            if self.use_fcnt:
                self.client.update_flexcontainer(self.anomaly_path, self.fcnt_key, attrs)
            else:
                self.client.create_cin(self.anomaly_path, res)
        except OneM2MError as e:
            print(f"[warn] anomaly 기록 실패: {e}")
        if self.use_fcnt and self.history_ready:
            try:
                self.client.create_cin(self.history_path, res, lbl=["anomaly", res["level"]])
            except OneM2MError as e:
                print(f"[warn] anomaly history 기록 실패: {e}")
        self._mirror_cin(self.o["anomaly"]["rn"], res, lbl=["anomaly", res["level"]])

    def _update_siren(self, res: dict) -> None:
        """레벨 상태로 사이렌 ON/OFF 판단 → 알람 group fanOut (CLAUDE.md: group 제어)."""
        if not self.group_ready:
            return
        on_rank = LEVEL_RANK.get(self.act_on_level, 2)
        want = any(LEVEL_RANK.get(lv, 0) >= on_rank for lv in self.levels.values())
        if want == self.siren_on:
            return
        self.siren_on = want
        cmd = {"cmd": "on" if want else "off", "ts": res["windowEnd"],
               "reason": {"subsystem": res["subsystem"], "level": res["level"],
                          "score": res["score"]}}
        try:
            self.client.fanout_create_cin(self.group_path, cmd, lbl=["alarm"])
            print(f"{res['windowEnd']} ALARM  group fanOut -> {cmd['cmd'].upper()} "
                  f"({res['subsystem']} {res['level']})")
        except OneM2MError as e:
            self.siren_on = not want       # 실패 시 되돌려 다음 윈도우에 재시도
            print(f"[warn] group fanOut 실패: {e}")

    def _on_link(self, ev: dict) -> None:
        extra = f" etaS={ev['etaS']}" if "etaS" in ev else \
            (f" regainEtaS={ev['regainEtaS']}" if "regainEtaS" in ev else "")
        print(f"{ev['ts']} LINK   {ev['type']:<15} mode={ev['mode']}{extra}")
        try:
            self.client.create_cin(self.link_path, ev, lbl=["linkForecast", ev["type"]])
        except OneM2MError as e:
            print(f"[warn] linkForecast 기록 실패: {e}")
        self._mirror_cin(self.link_path.rsplit("/", 1)[-1], ev, lbl=["linkForecast", ev["type"]])

    def _on_prog(self, ev: dict) -> None:
        print(f"{ev['ts']} PROG   {ev['type']:<15} {ev['channel']} eta={ev['etaS']}s "
              f"(-> {ev['threshold']})")
        try:
            self.client.create_cin(self.prog_path, ev, lbl=["prognosis", ev["type"]])
        except OneM2MError as e:
            print(f"[warn] prognosis 기록 실패: {e}")
        self._mirror_cin(self.prog_path.rsplit("/", 1)[-1], ev, lbl=["prognosis", ev["type"]])

    def _on_summary(self, summary: dict) -> None:
        try:
            self.client.create_cin(self.summary_path, summary)
            print(f"{summary['windowEnd']} summary CIN ({len(summary['stats'])} sensors)")
        except OneM2MError as e:
            print(f"[warn] summary 기록 실패: {e}")
        self._mirror_cin(self.o["summary"]["rn"], summary)

    # ---- 실행 ----

    def run(self) -> None:
        server = None
        if self.poll_enabled:
            print(f"폴링 수집 모드 (interval {self.poll_interval:g}s, 읽기 "
                  f"{self.poll_read_origin or self.client.originator})")
            while True:                             # CSE가 늦게 떠도(재부팅 순서 등) 기다림
                try:
                    self.setup_resources()
                    break
                except (OneM2MError, OSError) as e:
                    print(f"[warn] CSE 연결 대기: {e} — 10초 후 재시도")
                    time.sleep(10.0)
            self._poll_seed()
        else:
            n = self.o["notification"]
            server = NotificationServer((n["host"], int(n["port"])), NotificationHandler)
            server.on_sgn = self.on_sgn
            threading.Thread(target=server.serve_forever, daemon=True).start()
            print(f"notification 서버: {self.poa_url}")
            self.setup_resources()                  # 서버가 떠 있어야 verification 응답 가능
        print("수신 대기 중 (Ctrl+C 종료)")
        try:
            tick = 0
            last_status = 0.0
            last_registry = 0.0
            while True:
                try:
                    if self.poll_enabled:
                        t0 = time.time()
                        self._poll_once()
                        time.sleep(max(0.0, self.poll_interval - (time.time() - t0)))
                    else:
                        time.sleep(1.0)
                    tick += 1
                    if tick % 10 == 0:                     # 미완 리소스 재시도
                        if not self.poll_enabled and self.pending_subs:
                            self._try_subscriptions()
                        self._try_group()
                    if self.status_path and time.time() - last_status >= self.status_period:
                        last_status = time.time()
                        self._write_status()
                    if self.registry_client \
                            and time.time() - last_registry >= self.registry_poll_s:
                        last_registry = time.time()
                        self._check_model_update()
                except OSError as e:                       # CSE 재시작 등 — 죽지 않고 재접속
                    print(f"[warn] 네트워크 오류: {e} — 5초 후 재시도")
                    time.sleep(5.0)
        except KeyboardInterrupt:
            print("\n종료 (AE/구독 리소스는 재사용을 위해 유지)")
        finally:
            if server:
                server.shutdown()


# ================================================================ 리플레이 모드

def run_replay(cfg: dict, bundles: dict, replay: str, max_hours: float | None,
               record_normal: bool) -> None:
    p = Path(replay)
    csv = p / "eval.csv" if p.is_dir() else p
    if not csv.exists():
        raise SystemExit(f"{csv} 없음 — 먼저 `python generator.py` 를 실행하세요")
    skip_normal = (not record_normal) and bool(cfg["onem2m"]["anomaly"].get("skip_normal", True))

    df = load_csv(csv)
    lf_cfg = cfg.get("link_forecast") or {}
    link_events: list[dict] = []
    forecaster = None
    if lf_cfg.get("enabled"):
        def _on_link(ev: dict) -> None:
            link_events.append(ev)
            print(f"{ev['ts']} LINK   {ev['type']:<15} mode={ev['mode']}"
                  + (f" etaS={ev['etaS']}" if "etaS" in ev else ""))
        forecaster = LinkForecaster(lf_cfg, _on_link)
    pg_cfg = cfg.get("prognosis") or {}
    prog_events: list[dict] = []
    prognoser = None
    if pg_cfg.get("enabled"):
        def _on_prog(ev: dict) -> None:
            prog_events.append(ev)
            print(f"{ev['ts']} PROG   {ev['type']:<15} {ev['channel']} eta={ev['etaS']}s")
        prognoser = TrendPrognoser(pg_cfg, cfg.get("thresholds") or {}, _on_prog)
    keep = set(SENSOR_KEYS) | {HATCH}
    if forecaster:
        keep.add(("BRIDGE", "position"))
    df = df[[(s, c) in keep for s, c in zip(df["subsystem"], df["container"])]]
    df = df.sort_values("t")
    if max_hours:
        t0 = df["t"].min()
        df = df[df["t"] < t0 + np.timedelta64(int(max_hours * 3600), "s")]

    RESULTS.mkdir(exist_ok=True)
    anom_path = RESULTS / "replay_anomalies.jsonl"
    summ_path = RESULTS / "replay_summary.jsonl"
    anom_f = anom_path.open("w", encoding="utf-8")
    summ_f = summ_path.open("w", encoding="utf-8")
    counts: dict[str, int] = {"normal": 0, "warn": 0, "critical": 0}
    written = {"anomaly": 0, "summary": 0}
    last_level: dict[str, str] = {}

    def on_result(res: dict) -> None:
        counts[res["level"]] += 1
        prev = last_level.get(res["subsystem"], "normal")
        if res["level"] != prev:
            print(f"{res['windowEnd']} {res['subsystem']:<6} {prev}->{res['level']:<8} "
                  f"score={res['score']:.3f} top={res['topFeatures'][0]['feature']}")
            last_level[res["subsystem"]] = res["level"]
        if skip_normal and res["level"] == "normal":
            return
        anom_f.write(json.dumps(res, ensure_ascii=False) + "\n")
        written["anomaly"] += 1

    def on_summary(summary: dict) -> None:
        summ_f.write(json.dumps(summary, ensure_ascii=False) + "\n")
        written["summary"] += 1

    pipeline = WindowPipeline(bundles, on_result, on_summary,
                              summary_s=int(cfg["onem2m"]["summary"].get("period_s", 60)))
    tic = time.perf_counter()
    rows = 0
    for r in df.itertuples(index=False):
        ts = r.t.to_pydatetime().replace(tzinfo=timezone.utc)
        if (r.subsystem, r.container) == ("BRIDGE", "position"):
            forecaster.update(float(r.vals["lat"]), float(r.vals["lon"]),
                              float(r.vals.get("sog", 0.0)), float(r.vals.get("cog", 0.0)),
                              ts.timestamp(), r.ts)
            continue
        if prognoser:
            prognoser.observe(r.subsystem, r.container, r.vals, ts.timestamp(), r.ts)
        pipeline.add(r.subsystem, r.container, r.vals, ts)
        rows += 1
    pipeline.flush()
    anom_f.close()
    summ_f.close()

    dur = time.perf_counter() - tic
    total_w = sum(pipeline.windows.values())
    print(f"\nreplay 완료: {rows:,} rows -> windows {pipeline.windows} "
          f"(skipped {sum(pipeline.skipped.values())}), {dur:.1f}s")
    print(f"levels: {counts} | 기록 anomaly {written['anomaly']} / summary {written['summary']}")
    if forecaster:
        link_path = RESULTS / "replay_link.jsonl"
        link_path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n"
                                     for e in link_events), encoding="utf-8")
        print(f"link events: {len(link_events)} -> {link_path.relative_to(ROOT)}")
    if prognoser:
        pg_path = RESULTS / "replay_prognosis.jsonl"
        pg_path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n"
                                   for e in prog_events), encoding="utf-8")
        print(f"prognosis events: {len(prog_events)} -> {pg_path.relative_to(ROOT)}")
    print(f"-> {anom_path.relative_to(ROOT)}, {summ_path.relative_to(ROOT)}")
    if total_w:
        print(f"평균 추론 {dur / total_w * 1000:.1f} ms/window (파이프라인 전체 기준)")


# ================================================================ main

def main() -> None:
    ap = argparse.ArgumentParser(description="ANCHOR AI 분석 AE (상세: README.md)")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--replay", metavar="PATH",
                    help="CSE 없이 CSV(파일 또는 data/eval 디렉터리)를 notification처럼 재생")
    ap.add_argument("--max-hours", type=float, default=None, help="replay 앞부분만(시간)")
    ap.add_argument("--record-normal", action="store_true",
                    help="skip_normal 설정을 무시하고 normal 레벨도 기록")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    features.set_profile(str(cfg.get("sensor_profile", "synthetic")))
    _build_sensor_keys()
    bundles = load_models((cfg.get("paths") or {}).get("models_dir"))

    if args.replay:
        run_replay(cfg, bundles, args.replay, args.max_hours, args.record_normal)
    else:
        app = LiveApp(cfg, bundles)
        if args.record_normal:
            app.skip_normal = False
        app.run()


if __name__ == "__main__":
    main()
