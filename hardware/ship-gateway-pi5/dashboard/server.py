#!/usr/bin/env python3
"""ANCHOR ship dashboard: camera MJPEG + latest sensor values read from the ship MN-CSE.

Runs on the ship Pi with the system python (picamera2 comes from apt).
  https://<pi>:8090/            dashboard page
  https://<pi>:8090/stream.mjpg camera stream
  https://<pi>:8090/api/state   JSON: latest CIN per container + short history
Sensor values are read from the CSE (GET .../<cnt>/la), not sniffed from MQTT,
so the page shows exactly what oneM2M has stored.
"""
import io
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
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from anchor.tls import host_tls_files, open_https, client_context, secure_server, require_https
CA, CERT, KEY = host_tls_files()

PORT = 8090
CSE = "https://127.0.0.1:8080/~/id-ship/cse-ship"
HEADERS = {"X-M2M-Origin": "CAnchorDashboard", "X-M2M-RVI": "3", "Accept": "application/json"}
CONTAINERS = [
    "bridge/attitude",  # position remains restricted to BRIDGE and ship core
    "engine/vibration", "engine/gas",
    "cargo/climate", "cargo/hatch",
]
# numeric fields kept as history for the sparklines: (container, key)
TRACKED = [
    ("bridge/attitude", "roll"), ("bridge/attitude", "pitch"),
    ("engine/vibration", "vib_rms_g"), ("engine/gas", "mq2_v"),
    ("cargo/climate", "temp_c"), ("cargo/climate", "rh"),
]
HISTORY_LEN = 600  # ~10 min at the 1 s attitude/vibration period (the page shows the last 5 min)
LINK_CNT = "sync/link"  # shore-link state written every 5 s by the sync agent (kept even while offline)
PAGE = (Path(__file__).parent / "index.html").read_bytes()
BOOT = time.time()  # lets the page notice a restart and re-open the camera stream

state = {"latest": {}, "history": {f"{c}.{k}": deque(maxlen=HISTORY_LEN) for c, k in TRACKED},
         "link_hist": deque(maxlen=200), "cse_ok": False}
state_lock = threading.Lock()


def parse_ct(ct):
    # "20261113T105842,556510" (UTC)
    base, _, frac = ct.partition(",")
    t = datetime.strptime(base, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).timestamp()
    return t + (float("0." + frac) if frac else 0.0)


def poll_cse():
    rqi = 0
    last_ri = {}
    while True:
        ok = True
        for cnt in CONTAINERS + [LINK_CNT]:
            rqi += 1
            req = urllib.request.Request(f"{CSE}/{cnt}/la", headers={**HEADERS, "X-M2M-RI": f"dash{rqi}"})
            try:
                with open_https(req, ca=CA, timeout=2) as r:
                    cin = json.load(r)["m2m:cin"]
            except urllib.error.HTTPError as e:
                if e.code != 404 and cnt != LINK_CNT:  # 404 = container exists but is still empty
                    ok = False
                continue
            except Exception:
                ok = False
                continue
            try:
                con = json.loads(cin["con"]) if isinstance(cin["con"], str) else cin["con"]
            except ValueError:
                con = {"raw": cin["con"]}
            ct = parse_ct(cin["ct"])
            with state_lock:
                state["latest"][cnt] = {"con": con, "ct": ct, "ri": cin.get("ri")}
                if last_ri.get(cnt) != cin.get("ri"):
                    last_ri[cnt] = cin.get("ri")
                    if cnt == LINK_CNT and con.get("state"):
                        state["link_hist"].append((round(ct, 2), con["state"]))
                    for c, k in TRACKED:
                        if c == cnt and isinstance(con.get(k), (int, float)):
                            state["history"][f"{c}.{k}"].append((round(ct, 2), con[k]))
        with state_lock:
            state["cse_ok"] = ok
        time.sleep(1)


class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.condition = threading.Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.last = time.time()
            self.condition.notify_all()


def camera_watchdog():
    """libcamera does not recover from a frontend timeout (loose ribbon / brown-out): the stream
    just stops. Exit so systemd (Restart=always) restarts us and re-opens the camera."""
    time.sleep(15)
    while True:
        last = getattr(camera_output, "last", 0)
        if time.time() - last > 5:
            print(f"camera stalled ({time.time() - last:.0f}s without a frame), restarting", flush=True)
            os._exit(1)
        time.sleep(1)


camera_output = None
camera_error = None


def start_camera():
    global camera_output, camera_error
    try:
        from picamera2 import Picamera2
        from picamera2.encoders import JpegEncoder
        from picamera2.outputs import FileOutput

        cam = Picamera2()
        cam.configure(cam.create_video_configuration(main={"size": (960, 720)}, controls={"FrameRate": 15}))
        camera_output = StreamingOutput()
        cam.start_recording(JpegEncoder(q=70), FileOutput(camera_output))
    except Exception as e:  # dashboard still works without a camera
        camera_error = str(e)
        print("camera disabled:", e, flush=True)


class Handler(server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_body(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_body(200, "text/html; charset=utf-8", PAGE)
        elif self.path == "/api/state":
            with state_lock:
                body = {
                    "now": time.time(),
                    "boot": BOOT,
                    "cse_ok": state["cse_ok"],
                    "link_hist": list(state["link_hist"]),
                    "camera": camera_error is None and camera_output is not None,
                    "camera_error": camera_error,
                    "latest": state["latest"],
                    "history": {k: list(v) for k, v in state["history"].items()},
                }
            self.send_body(200, "application/json", json.dumps(body).encode())
        elif self.path.startswith("/stream.mjpg"):
            if camera_output is None:
                self.send_body(503, "text/plain", f"camera unavailable: {camera_error}".encode())
                return
            self.send_response(200)
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=FRAME")
            self.end_headers()
            try:
                while True:
                    with camera_output.condition:
                        camera_output.condition.wait()
                        frame = camera_output.frame
                    self.wfile.write(b"--FRAME\r\nContent-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode())
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.send_body(404, "text/plain", b"not found")


class LiveFeed:
    """Fast path: relays MQTTS telemetry (anchor/live/#, 20 Hz attitude) to browsers via SSE.
    Uses the mosquitto_sub CLI so the system python needs no MQTT library."""

    def __init__(self):
        self.msg = None
        self.seq = 0
        self.cond = threading.Condition()

    def run(self):
        import subprocess
        while True:
            p = subprocess.Popen(["mosquitto_sub", "-h", "127.0.0.1", "-p", "8883",
                                  "--cafile", str(CA), "--cert", str(CERT), "--key", str(KEY),
                                  "-t", "anchor/live/#", "-v"],
                                 stdout=subprocess.PIPE, text=True)
            for line in p.stdout:
                topic, _, payload = line.strip().partition(" ")
                with self.cond:
                    self.msg = json.dumps({"topic": topic, "data": json.loads(payload or "null"), "t": time.time()})
                    self.seq += 1
                    self.cond.notify_all()
            time.sleep(2)  # broker restarted: reconnect


live = LiveFeed()


def _live_stream(handler):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    seen = live.seq
    try:
        while True:
            with live.cond:
                live.cond.wait_for(lambda: live.seq != seen, timeout=10)
                msg, seen = live.msg, live.seq
            # keep-alive comment when nothing arrived for 10 s
            handler.wfile.write((f"data: {msg}\n\n" if msg else ": ping\n\n").encode())
            handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass


_orig_get = Handler.do_GET


def _do_GET(self):
    if self.path == "/api/live":
        _live_stream(self)
    else:
        _orig_get(self)


Handler.do_GET = _do_GET


def send_command(ae, con):
    """Create a CIN in <ae>/command; the node's subscription delivers it to the ESP32."""
    body = json.dumps({"m2m:cin": {"con": con}}).encode()
    req = urllib.request.Request(
        f"{CSE}/{ae}/command", data=body, method="POST",
        headers={**HEADERS, "X-M2M-RI": f"cmd{time.time():.0f}", "Content-Type": "application/json;ty=4"})
    with open_https(req, ca=CA, timeout=3) as r:
        return r.status


def _do_POST(self):
    commands = {"/api/bridge/zero": ("bridge", "zero"), "/api/bridge/unzero": ("bridge", "unzero")}
    if self.path not in commands:
        self.send_body(404, "text/plain", b"not found")
        return
    try:
        status = send_command(*commands[self.path])
        self.send_body(200, "application/json", json.dumps({"ok": status == 201}).encode())
    except Exception as e:
        self.send_body(502, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())


Handler.do_POST = _do_POST


class ThreadingServer(ThreadingMixIn, server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    start_camera()
    if camera_output is not None:
        threading.Thread(target=camera_watchdog, daemon=True).start()
    threading.Thread(target=poll_cse, daemon=True).start()
    threading.Thread(target=live.run, daemon=True).start()
    print(f"ANCHOR dashboard on :{PORT}", flush=True)
    secure_server(ThreadingServer(("0.0.0.0", PORT), Handler), CERT, KEY).serve_forever()
