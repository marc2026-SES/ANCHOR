#!/usr/bin/env python3
"""ANCHOR ship-to-shore sync agent (A-2) + link monitor (A-4). Runs on the ship Pi, stdlib only.

  ship MN-CSE (ACME)  --oneM2M SUB, HTTP notify-->  this agent  --HTTP CIN-->  shore IN-CSE (tinyIoT)
                                                     |
                                                     +-- SQLite store-and-forward queue (survives
                                                         power loss and link outages, FIFO replay)

Every CIN created in a watched ship container is replicated to
    /cse-in/ship-gw/<node>/<sensor>
on the IN-CSE with its original con plus "ship_ct" (creation time on the ship) and "fwd_delay_s"
(how long it waited before reaching shore, > 0 means it was buffered through an outage).

The link monitor probes the IN-CSE every PROBE_S seconds and writes a status CIN to
cse-ship/sync/link (state ONLINE/DEGRADED/OFFLINE, RTT, loss, Wi-Fi level, backlog). That container
is watched too, so the shore also receives the link history, including what happened while offline.
"""
import http.client
import json
import os
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------- configuration ----------
SHIP = ("127.0.0.1", 8080)
SHIP_BASE = "/~/id-ship/cse-ship"
SHORE = (os.environ.get("ANCHOR_SHORE_HOST", "10.251.196.64"), 8080)
SHORE_BASE = "/~/id-in/cse-in"
SHORE_AE, SHORE_ORIG = "ship-gw", "CShipGW"
SHIP_AE, SHIP_ORIG = "sync", "CSync"
NOTIFY_PORT = 9091
WATCH = [
    "bridge/attitude", "bridge/position",
    "engine/vibration", "engine/gas",
    "cargo/climate", "cargo/hatch",
    "sync/link",
]
SUB_RN = "sub-sync"
SHORE_MNI = 5000            # CINs kept per container on the shore
QUEUE_MAX = 200_000         # oldest rows dropped beyond this (~14 h of all sensors)
PROBE_S = 5
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "queue.db")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def parse_ct(ct):
    """oneM2M timestamp '20261115T074607,561' (UTC) -> epoch seconds."""
    base, _, frac = ct.partition(",")
    t = datetime.strptime(base, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).timestamp()
    return t + (float("0." + frac) if frac else 0.0)


_rqi = 0
_rqi_lock = threading.Lock()


def request(host, method, path, origin, body=None, ty=None, timeout=3.0):
    """One oneM2M HTTP request. Returns (status, json|None). Raises OSError on network failure."""
    global _rqi
    with _rqi_lock:
        _rqi += 1
        rqi = f"sync{_rqi}"
    headers = {"X-M2M-Origin": origin, "X-M2M-RI": rqi, "X-M2M-RVI": "3", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = f"application/json;ty={ty}"
    conn = http.client.HTTPConnection(*host, timeout=timeout)
    try:
        conn.request(method, path, body=data, headers=headers)
        r = conn.getresponse()
        raw = r.read()
        try:
            return r.status, json.loads(raw) if raw else None
        except ValueError:
            return r.status, None
    finally:
        conn.close()


def created_or_exists(status):
    return status in (200, 201, 409)


# ---------- durable queue ----------
class Queue:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")   # crash-safe on power loss
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS q (id INTEGER PRIMARY KEY, path TEXT, con TEXT, ct TEXT)")
        self.lock = threading.Lock()
        self.ready = threading.Event()

    def put(self, path, con, ct):
        with self.lock:
            self.db.execute("INSERT INTO q (path, con, ct) VALUES (?,?,?)", (path, con, ct))
            n = self.db.execute("SELECT COUNT(*) FROM q").fetchone()[0]
            if n > QUEUE_MAX:
                self.db.execute("DELETE FROM q WHERE id IN (SELECT id FROM q ORDER BY id LIMIT ?)", (n - QUEUE_MAX,))
        self.ready.set()

    def head(self):
        with self.lock:
            return self.db.execute("SELECT id, path, con, ct FROM q ORDER BY id LIMIT 1").fetchone()

    def done(self, row_id):
        with self.lock:
            self.db.execute("DELETE FROM q WHERE id=?", (row_id,))

    def size(self):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM q").fetchone()[0]


queue = Queue(DB)


# ---------- link monitor (A-4) ----------
class Link:
    def __init__(self):
        self.window = deque(maxlen=12)      # last 12 probes (1 min): True/False
        self.rtt_ms = None
        self.fails_in_row = 0
        self.state = "UNKNOWN"
        self.since = time.time()
        self.last_ok = None
        self.sent_total = 0
        self.replayed_total = 0             # forwarded with fwd_delay_s > 10 (i.e. buffered)
        self.lock = threading.Lock()

    def record(self, ok, rtt_ms=None):
        with self.lock:
            self.window.append(ok)
            if ok:
                self.fails_in_row = 0
                self.rtt_ms = rtt_ms if rtt_ms is not None else self.rtt_ms
                self.last_ok = time.time()
            else:
                self.fails_in_row += 1
            loss = self.window.count(False) / len(self.window)
            if self.fails_in_row >= 2:
                new = "OFFLINE"
            elif loss > 0 or (self.rtt_ms or 0) > 800:
                new = "DEGRADED"
            else:
                new = "ONLINE"
            changed = new != self.state
            prev, prev_since = self.state, self.since
            if changed:
                self.state, self.since = new, time.time()
        if changed:
            log(f"link {prev} -> {new}")
            write_link_status(event=f"{prev}->{new}", prev_duration_s=round(time.time() - prev_since, 1))

    @property
    def online(self):
        return self.state in ("ONLINE", "DEGRADED", "UNKNOWN")

    def snapshot(self):
        with self.lock:
            loss = self.window.count(False) / len(self.window) if self.window else None
            return {
                "state": self.state,
                "since_s": round(time.time() - self.since, 1),
                "rtt_ms": self.rtt_ms,
                "loss_pct": round(loss * 100) if loss is not None else None,
                "last_ok_s": round(time.time() - self.last_ok, 1) if self.last_ok else None,
                "sent_total": self.sent_total,
                "replayed_total": self.replayed_total,
            }


link = Link()


def wifi_level():
    """Ship Pi Wi-Fi signal in dBm from /proc/net/wireless (None if not associated)."""
    try:
        for line in open("/proc/net/wireless"):
            if line.strip().startswith("wlan0:"):
                return int(float(line.split()[3]))
    except (OSError, ValueError, IndexError):
        pass
    return None


def write_link_status(**extra):
    con = {**link.snapshot(), "wifi_dbm": wifi_level(), "backlog": queue.size(), **extra,
           "ts": int(time.time() * 1000)}
    try:
        request(SHIP, "POST", f"{SHIP_BASE}/{SHIP_AE}/link", SHIP_ORIG,
                {"m2m:cin": {"cnf": "application/json:0", "con": json.dumps(con)}}, ty=4)
    except OSError as e:
        log("link status write failed:", e)


def probe_loop():
    while True:
        t0 = time.time()
        try:
            # any HTTP answer proves the path to the IN-CSE works (404 = our AE not created yet)
            status, _ = request(SHORE, "GET", f"{SHORE_BASE}/{SHORE_AE}", SHORE_ORIG, timeout=2.5)
            link.record(status < 500, round((time.time() - t0) * 1000))
        except OSError:
            link.record(False)
        write_link_status()
        time.sleep(max(0.0, PROBE_S - (time.time() - t0)))


# ---------- ship side: AE, link container, subscriptions ----------
def ensure_ship():
    s, _ = request(SHIP, "GET", f"{SHIP_BASE}/{SHIP_AE}", SHIP_ORIG)
    if s != 200:  # ACME answers 403 "already registered" to a repeated AE create, so look first
        s, _ = request(SHIP, "POST", SHIP_BASE, SHIP_ORIG,
                       {"m2m:ae": {"rn": SHIP_AE, "api": "Nanchor.sync", "rr": True, "srv": ["3"],
                                   "poa": [f"http://127.0.0.1:{NOTIFY_PORT}"]}}, ty=2)
        if not created_or_exists(s):
            return False
    request(SHIP, "POST", f"{SHIP_BASE}/{SHIP_AE}", SHIP_ORIG, {"m2m:cnt": {"rn": "link", "mni": 2000}}, ty=3)
    ok = True
    for cnt in WATCH:
        rn = sub_rn.get(cnt, SUB_RN)
        s, _ = request(SHIP, "GET", f"{SHIP_BASE}/{cnt}/{rn}", SHIP_ORIG)
        if s == 200:
            continue
        s, body = request(SHIP, "POST", f"{SHIP_BASE}/{cnt}", SHIP_ORIG, {"m2m:sub": {
            "rn": rn,
            "nu": [f"http://127.0.0.1:{NOTIFY_PORT}/notify/{cnt}"],
            "enc": {"net": [3]},  # create of direct child
            "nct": 1,
        }}, ty=23)
        if created_or_exists(s):
            sub_created[cnt] = time.time()
            log(f"subscribed {cnt}")
        else:
            ok = False  # container not there yet (node offline / fresh DB): retried later
    return ok


last_notified = {}  # container -> ct of the last CIN we were notified about
started_at = time.time()
sub_rn = {}         # container -> subscription name in use, when it differs from SUB_RN
sub_created = {}    # container -> when we created its subscription


def heal_silent_subs():
    """A subscription can survive in ACME but stop notifying (seen after ACME and this agent restarted
    together). If a container's newest CIN is >30 s newer than the last one notified to us, recreate
    its subscription."""
    for cnt in WATCH:
        s, body = request(SHIP, "GET", f"{SHIP_BASE}/{cnt}/la", SHIP_ORIG)
        if s != 200:
            continue
        ct = (body or {}).get("m2m:cin", {}).get("ct")
        seen = last_notified.get(cnt)
        if not ct or (seen and parse_ct(ct) - parse_ct(seen) < 30):
            continue
        if not seen and time.time() - max(started_at, sub_created.get(cnt, 0)) < 60:
            continue  # agent or subscription just started: give it a moment to deliver
        log(f"subscription on {cnt} is silent (newest {ct}, last notified {seen}), recreating")
        rn = sub_rn.get(cnt, SUB_RN)
        s, _ = request(SHIP, "DELETE", f"{SHIP_BASE}/{cnt}/{rn}", SHIP_ORIG)
        if s not in (200, 404):
            # ACME can lose the subscription-table entry (TinyDB write cache on restart) and then
            # refuses to delete the leftover resource; subscribe again under a fresh name instead.
            sub_rn[cnt] = f"{SUB_RN}-{int(time.time())}"
            log(f"could not delete {cnt}/{rn} ({s}), resubscribing as {sub_rn[cnt]}")
        last_notified.pop(cnt, None)
    ensure_ship()


def ship_maintenance():
    """Re-check the AE and subscriptions every minute: an ACME DB reset (power loss) drops them,
    and a subscription can also go silent."""
    while True:
        time.sleep(60)
        try:
            ensure_ship()
            heal_silent_subs()
        except OSError as e:
            log("ship CSE unreachable:", e)


class NotifyHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self):
        self.send_response(200)
        self.send_header("X-M2M-RSC", "2000")
        self.send_header("X-M2M-RI", self.headers.get("X-M2M-RI", ""))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0)
        path = self.path.removeprefix("/notify/").strip("/")
        try:
            sgn = json.loads(body).get("m2m:sgn", {})
        except ValueError:
            sgn = {}
        cin = (sgn.get("nev") or {}).get("rep", {}).get("m2m:cin")
        if cin and path in WATCH:
            last_notified[path] = cin.get("ct", "")
            con = cin.get("con")
            queue.put(path, con if isinstance(con, str) else json.dumps(con), cin.get("ct", ""))
        self.reply()  # also answers the verification request (vrq)


# ---------- forwarder (A-2) ----------
shore_paths = set()


class ShoreError(Exception):
    """The IN-CSE answered, but refused the request (not a link problem)."""


def ensure_shore_path(path):
    if path in shore_paths:
        return True
    s, _ = request(SHORE, "GET", f"{SHORE_BASE}/{SHORE_AE}", SHORE_ORIG)
    if s != 200:  # ACME answers 403 "already registered" to a repeated AE create, so look first
        s, _ = request(SHORE, "POST", SHORE_BASE, SHORE_ORIG,
                       {"m2m:ae": {"rn": SHORE_AE, "api": "Nanchor.shipgw", "rr": False, "srv": ["3"]}}, ty=2)
        if not created_or_exists(s):
            return False
    parent = f"{SHORE_BASE}/{SHORE_AE}"
    parts = path.split("/")
    for i, part in enumerate(parts):
        body = {"rn": part}
        if i == len(parts) - 1:
            body["mni"] = SHORE_MNI
        s, _ = request(SHORE, "POST", parent, SHORE_ORIG, {"m2m:cnt": body}, ty=3)
        if not created_or_exists(s):
            return False
        parent += "/" + part
    shore_paths.add(path)
    return True


def forward_loop():
    backoff = 1
    while True:
        row = queue.head()
        if row is None:
            queue.ready.clear()
            queue.ready.wait(5)
            continue
        if not link.online:
            time.sleep(1)  # the probe decides when the link is back
            continue
        row_id, path, con, ct = row
        try:
            payload = json.loads(con)
            if not isinstance(payload, dict):
                payload = {"value": payload}
        except ValueError:
            payload = {"raw": con}
        delay = round(time.time() - parse_ct(ct), 1) if ct else None
        payload.update({"ship_ct": ct, "fwd_delay_s": delay})
        try:
            if not ensure_shore_path(path):
                raise ShoreError("could not create shore containers")
            s, _ = request(SHORE, "POST", f"{SHORE_BASE}/{SHORE_AE}/{path}", SHORE_ORIG,
                           {"m2m:cin": {"cnf": "application/json:0", "con": json.dumps(payload)}}, ty=4)
            if s == 404:            # shore DB was reset: rebuild the containers and retry
                shore_paths.discard(path)
                continue
            if s not in (200, 201):
                raise ShoreError(f"shore answered {s}")
        except (OSError, ShoreError) as e:
            if isinstance(e, OSError):  # only network failures say anything about the link
                link.record(False)
            log(f"forward failed ({e}), backlog={queue.size()}, retry in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 10)
            continue
        backoff = 1
        queue.done(row_id)
        with link.lock:
            link.sent_total += 1
            if delay and delay > 10:
                link.replayed_total += 1


def main():
    log(f"ANCHOR sync: ship {SHIP[0]}:{SHIP[1]} -> shore {SHORE[0]}:{SHORE[1]}, backlog={queue.size()}")
    server = ThreadingHTTPServer(("127.0.0.1", NOTIFY_PORT), NotifyHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    while True:  # wait for the ship CSE at boot
        try:
            ensure_ship()
            break
        except OSError:
            time.sleep(3)
    threading.Thread(target=ship_maintenance, daemon=True).start()
    threading.Thread(target=probe_loop, daemon=True).start()
    forward_loop()


if __name__ == "__main__":
    main()
