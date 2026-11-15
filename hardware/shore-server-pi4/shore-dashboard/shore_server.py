#!/usr/bin/env python3
"""ANCHOR shore control dashboard (runs on the shore Pi next to the IN-CSE). Stdlib only.

Reads ONLY what has been replicated to the shore IN-CSE (cse-in/ship-gw/...), never the ship
directly, so a broken ship-shore link looks exactly like it does to a real shore operator:
values freeze, then the buffered data arrives after the link returns and fills the gap.

  http://<pi4>:8090/            dashboard page
  http://<pi4>:8090/api/state   JSON: link judgement, latest values, 10-min series, events
  http://<pi4>:8090/stream.mjpg ship camera, relayed from the ship Pi (freezes when the link drops)

Every replicated CIN reaches us through a oneM2M subscription (HTTP notification to this process),
so the backfill burst after an outage is captured CIN by CIN, not sampled.
"""
import http.client
import json
import os
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime, timezone
from http import server
from pathlib import Path
from socketserver import ThreadingMixIn

PORT = 8090
NOTIFY_PORT = 8091
CSE_HOST = ("127.0.0.1", 8080)
GW = "/~/id-in/cse-in/ship-gw"
ORIG = "CAdmin"  # the shore CSE keeps ACP checks on; the dashboard reads as the admin
SHIP_CAMERA = os.environ.get("ANCHOR_SHIP_CAMERA", "http://10.251.196.250:8090/stream.mjpg")
CONTAINERS = [
    "bridge/attitude", "bridge/position",
    "engine/vibration", "engine/gas",
    "cargo/climate", "cargo/hatch",
    "sync/link",
]
SUB_RN = "sub-shore-dash"
SERIES = {  # chart key -> (container, field)
    "roll": ("bridge/attitude", "roll"), "pitch": ("bridge/attitude", "pitch"),
    "vib": ("engine/vibration", "vib_rms_g"), "gas": ("engine/gas", "mq2_v"),
    "temp": ("cargo/climate", "temp_c"), "rh": ("cargo/climate", "rh"),
    "rtt": ("sync/link", "rtt_ms"),
}
WINDOW_S = 900          # series kept for the charts (15 min)
OUTAGE_GAP_S = 12       # no replicated CIN for this long = link judged down (normal gap is ~1 s)
DELAYED_S = 10          # a CIN that took longer than this to reach shore was buffered on the ship
PAGE = Path(__file__).with_name("shore.html")

lock = threading.Lock()
latest = {}                                     # container -> {"con", "ship_t", "delay"}
series = {k: deque() for k in SERIES}           # key -> [(ship_t, value, delayed)]
events = deque(maxlen=60)                       # {"t", "kind", "text"}
outages = deque(maxlen=20)                      # {"start", "end"|None}  (shore arrival time)
NODE_SEQ = {n: deque(maxlen=6000) for n in ("bridge", "engine", "cargo")}  # (ship_t, seq) per ESP32
link = {"last_arrival": None, "phase": "WAITING", "replayed": 0, "max_delay": 0.0, "cur_delay": None,
        "outage_replayed": 0}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def parse_ct(ct):
    base, _, frac = ct.partition(",")
    t = datetime.strptime(base, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).timestamp()
    return t + (float("0." + frac) if frac else 0.0)


def to_ct(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y%m%dT%H%M%S")


def event(kind, text, t=None):
    events.appendleft({"t": t or time.time(), "kind": kind, "text": text})
    log(f"[{kind}] {text}")


_rqi = 0


def cse(method, path, body=None, ty=None, timeout=5):
    global _rqi
    _rqi += 1
    h = {"X-M2M-Origin": ORIG, "X-M2M-RI": f"dash{_rqi}", "X-M2M-RVI": "3", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = f"application/json;ty={ty}"
    c = http.client.HTTPConnection(*CSE_HOST, timeout=timeout)
    try:
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        return r.status, (json.loads(raw) if raw else None)
    finally:
        c.close()


# ---------- ingest ----------
def ingest(cnt, cin, live=True):
    """Store one replicated CIN. live=False for the history loaded at startup (no link logic)."""
    try:
        con = json.loads(cin["con"]) if isinstance(cin.get("con"), str) else cin.get("con") or {}
    except ValueError:
        return
    if not isinstance(con, dict):
        return
    arrive = parse_ct(cin["ct"]) if "ct" in cin else time.time()
    ship_t = parse_ct(con["ship_ct"]) if con.get("ship_ct") else arrive
    delay = con.get("fwd_delay_s") or 0.0
    delayed = delay > DELAYED_S
    with lock:
        if live:
            now = time.time()
            last = link["last_arrival"]
            if link["phase"] == "OUTAGE" or (last and now - last > OUTAGE_GAP_S):
                gap = now - last if last else 0
                if outages and outages[-1]["end"] is None:
                    outages[-1]["end"] = now
                else:
                    outages.append({"start": last, "end": now})
                link["phase"] = "CATCHING_UP"
                link["outage_replayed"] = 0
                event("recover", f"선박 링크 복구 · 두절 {gap:.0f}초 · 선박에 보관된 데이터 수신 시작")
            link["last_arrival"] = now
            link["cur_delay"] = delay
            if delayed:
                link["replayed"] += 1
                link["outage_replayed"] += 1
                link["max_delay"] = max(link["max_delay"], delay)
            if link["phase"] == "CATCHING_UP" and delay < 5:
                link["phase"] = "ONLINE"
                missing = seq_gaps(outages[-1]) if outages else None
                check = "" if missing is None else " · 순번 검증: 누락 없음" if missing == 0 else f" · 순번 검증: {missing}건 누락"
                event("synced", f"동기화 완료 · 보관 데이터 {link['outage_replayed']}건 복구{check}")
            elif link["phase"] == "WAITING":
                link["phase"] = "CATCHING_UP" if delayed else "ONLINE"
        node = cnt.split("/")[0]
        if node in NODE_SEQ and isinstance(con.get("seq"), int):
            NODE_SEQ[node].append((ship_t, con["seq"]))
        prev = latest.get(cnt)
        if not prev or ship_t >= prev["ship_t"]:
            latest[cnt] = {"con": con, "ship_t": ship_t, "delay": delay}
        for key, (c, field) in SERIES.items():
            v = con.get(field)
            if c == cnt and isinstance(v, (int, float)):
                s = series[key]
                s.append((round(ship_t, 2), v, delayed))
                if delayed and len(s) > 1 and s[-2][0] > ship_t:  # backfill arrived out of order
                    series[key] = s = deque(sorted(s))
                while s and s[0][0] < time.time() - WINDOW_S:
                    s.popleft()
        if live and cnt == "sync/link" and con.get("event") and not con["event"].startswith("UNKNOWN"):
            ev = con["event"]
            event("ship", f"선박 기록 · 링크 {ev.replace('->', ' → ')}"
                          + (f" (이전 상태 {con.get('prev_duration_s', 0):.0f}초)" if con.get("prev_duration_s") else ""),
                  ship_t)


def seq_gaps(outage):
    """Count CINs that never reached shore around an outage. Each ESP32 numbers all of its CINs with
    one running seq (shared by its containers), so within a node the received seqs must be contiguous.
    Checked from 30 s before the outage to its end (ship time); None if there is too little data."""
    lo, hi = (outage["start"] or 0) - 30, outage["end"] or time.time()
    missing, checked = 0, False
    for pts in NODE_SEQ.values():
        seqs = sorted({s for t, s in pts if lo <= t <= hi})
        if len(seqs) < 2:
            continue
        checked = True
        missing += sum(b - a - 1 for a, b in zip(seqs, seqs[1:]) if 0 < b - a < 10000)  # ignore reboots (seq reset)
    return missing if checked else None


def watchdog():
    """Declare an outage when replicated data stops arriving."""
    while True:
        with lock:
            last = link["last_arrival"]
            if last and link["phase"] != "OUTAGE" and time.time() - last > OUTAGE_GAP_S:
                link["phase"] = "OUTAGE"
                outages.append({"start": last, "end": None})
                event("outage", "선박 통신 두절 · 선박은 자체 운용 중, 데이터는 선박에 보관됨")
        time.sleep(1)


def load_history():
    cra = to_ct(time.time() - WINDOW_S)
    n = 0
    for cnt in CONTAINERS:
        try:
            s, body = cse("GET", f"{GW}/{cnt}?rcn=4&ty=4&cra={cra}&lim=2000", timeout=15)
        except OSError:
            continue
        if s != 200:
            continue
        for cin in (body or {}).get("m2m:cnt", {}).get("m2m:cin", []):
            ingest(cnt, cin, live=False)
            n += 1
    with lock:
        for k in series:
            series[k] = deque(sorted(series[k]))
    log(f"history loaded: {n} CINs")


# ---------- subscriptions ----------
def ensure_subs():
    for cnt in CONTAINERS:
        s, _ = cse("GET", f"{GW}/{cnt}/{SUB_RN}")
        if s == 200:
            continue
        s, body = cse("POST", f"{GW}/{cnt}", {"m2m:sub": {
            "rn": SUB_RN, "nu": [f"http://127.0.0.1:{NOTIFY_PORT}/notify/{cnt}"],
            "enc": {"net": [3]}, "nct": 1}}, ty=23)
        if s in (200, 201):
            log(f"subscribed ship-gw/{cnt}")


def sub_maintenance():
    while True:  # the containers appear when the first replicated CIN arrives; re-check every 30 s
        try:
            ensure_subs()
        except OSError as e:
            log("shore CSE unreachable:", e)
        time.sleep(30)


class NotifyHandler(server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0)
        cnt = self.path.removeprefix("/notify/").strip("/")
        try:
            cin = json.loads(body).get("m2m:sgn", {}).get("nev", {}).get("rep", {}).get("m2m:cin")
        except (ValueError, AttributeError):
            cin = None
        if cin and cnt in CONTAINERS:
            ingest(cnt, cin)
        self.send_response(200)
        self.send_header("X-M2M-RSC", "2000")
        self.send_header("X-M2M-RI", self.headers.get("X-M2M-RI", ""))
        self.send_header("Content-Length", "0")
        self.end_headers()


# ---------- web ----------
def state():
    now = time.time()
    with lock:
        last = link["last_arrival"]
        ship_link = latest.get("sync/link", {}).get("con", {})
        return {
            "now": now,
            "link": {
                "phase": link["phase"],
                "age": round(now - last, 1) if last else None,
                "cur_delay": link["cur_delay"],
                "replayed": link["replayed"],
                "outage_replayed": link["outage_replayed"],
                "max_delay": link["max_delay"],
                "ship": ship_link,
                "outages": list(outages),
            },
            "latest": {k: {**v, "age": round(now - v["ship_t"], 1)} for k, v in latest.items()},
            "series": {k: [p for p in v if p[0] > now - WINDOW_S] for k, v in series.items()},
            "events": sorted(events, key=lambda e: e["t"], reverse=True)[:30],
        }


class Handler(server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def body(self, code, ctype, data):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.body(200, "text/html; charset=utf-8", PAGE.read_bytes())
        elif self.path == "/api/state":
            self.body(200, "application/json", json.dumps(state()).encode())
        elif self.path.startswith("/stream.mjpg"):
            self.relay_camera()
        else:
            self.body(404, "text/plain", b"not found")

    def relay_camera(self):
        """Pass the ship camera through the shore Pi: it rides the same ship-shore link."""
        try:
            up = urllib.request.urlopen(SHIP_CAMERA, timeout=4)
        except OSError as e:
            self.body(503, "text/plain", f"ship camera unreachable: {e}".encode())
            return
        self.send_response(200)
        self.send_header("Content-Type", up.headers.get("Content-Type", "multipart/x-mixed-replace; boundary=FRAME"))
        self.send_header("Cache-Control", "no-cache, private")
        self.end_headers()
        try:
            while True:
                chunk = up.read1(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except OSError:  # link dropped (read timeout) or the browser closed the tab
            pass
        finally:
            up.close()


class ThreadingServer(ThreadingMixIn, server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    notify = ThreadingServer(("127.0.0.1", NOTIFY_PORT), NotifyHandler)
    threading.Thread(target=notify.serve_forever, daemon=True).start()
    try:
        load_history()
    except Exception as e:
        log("history load failed:", e)
    threading.Thread(target=sub_maintenance, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    log(f"ANCHOR shore dashboard on :{PORT}")
    ThreadingServer(("0.0.0.0", PORT), Handler).serve_forever()
