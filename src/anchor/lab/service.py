from __future__ import annotations

import hmac
import json
import math
import signal
import ssl
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .client import Client, OneM2MError, resource
from .config import API_PORT, BIND_HOST, ROLE, RUNTIME, atomic_json, read_json, server_cert
from .engine import Engine
from .models import record_name
from .resources import bootstrap, latest, write_state


class ShoreState:
    """Shore can accept commands with the ship powered off or unreachable."""
    def __init__(self):
        self.bootstrapped = False
        self.done = threading.Event()
        self.wake = threading.Event()
        self.errors = deque(maxlen=32)

    def status(self):
        return {"role": "shore", "state": {"phase": "READY" if self.bootstrapped else "STARTING"},
                "metrics": latest(Client("shore"), "shore/anchor/deliveryMetrics", {}),
                "metricsSource": "last report received from ship; inspect updatedAt for freshness",
                "errors": list(self.errors)}


def serve():
    engine = ShoreState() if ROLE == "shore" else Engine()
    token = (RUNTIME / "api-token").read_text().strip()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, data):
            payload = json.dumps(data).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-M2M-RSC", "2000" if code < 300 else "4000")
            self.send_header("X-M2M-RI", self.headers.get("X-M2M-RI", ""))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path != "/health" and not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                return self.send(401, {"error": "Invalid API token"})
            if self.path == "/health":
                self.send(200, {"ok": True, "role": ROLE, "bootstrapped": engine.bootstrapped})
            elif self.path == "/status":
                self.send(200, {"role": ROLE, **engine.status()})
            else:
                self.send(404, {"error": "Not found"})

        def do_POST(self):
            if self.path != "/notify/telemetry" and not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                return self.send(401, {"error": "Invalid API token"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 1_048_576:
                    return self.send(413, {"error": "Invalid body size"})
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError("Expected a JSON object")
                if ROLE == "shore" and self.path in ("/notify/telemetry", "/scenario"):
                    return self.send(405, {"error": "This endpoint belongs to the ship role"})
                if ROLE == "ship" and self.path == "/commands":
                    return self.send(405, {"error": "Submit commands on the shore Pi"})
                if self.path == "/notify/telemetry":
                    # CSE notification verification is acknowledged without mutating state.
                    if not body.get("m2m:sgn", {}).get("vrq"):
                        engine.notify(body)
                    return self.send(200, {})
                if self.path == "/bootstrap":
                    data = bootstrap()
                    engine.bootstrapped = True
                    engine.wake.set()
                    return self.send(200, data)
                if self.path == "/scenario":
                    if body.get("zone") not in ("coastal", "offshore", "shadow"):
                        raise ValueError("zone must be coastal, offshore or shadow")
                    atomic_json(RUNTIME / "scenario.json", body)
                    return self.send(200, {"mode": "local-link-simulator", **body})
                if self.path == "/commands":
                    if body.get("target") not in ("fan", "buzzer", "safetyGroup") or type(body.get("desiredState")) is not bool:
                        raise ValueError("Invalid target or desiredState")
                    ttl = float(body.get("ttl", 60))
                    if not math.isfinite(ttl) or not 0 < ttl <= 3600:
                        raise ValueError("ttl must be in (0, 3600]")
                    command_id = body.get("commandId", uuid.uuid4().hex)
                    if not isinstance(command_id, str) or not 1 <= len(command_id) <= 128:
                        raise ValueError("commandId must be a string of 1..128 characters")
                    try:
                        existing = json.loads(resource(Client("shore").get(
                            "shore/anchor/commands/" + record_name(command_id)))["con"])
                    except OneM2MError as exc:
                        if exc.rsc != 4004:
                            raise
                    else:
                        if any(existing[k] != body[k] for k in ("target", "desiredState")):
                            return self.send(409, {"error": "commandId already has a different payload"})
                        return self.send(200, existing)
                    command = {"commandId": command_id, "target": body["target"],
                               "desiredState": body["desiredState"], "issuedAt": time.time(), "expiresAt": time.time() + ttl}
                    Client("shore").put_record("shore/anchor/commands", record_name(command["commandId"]), command)
                    def hint():
                        try:
                            Client("shore", timeout=7).notify("shore/shipCommands", {"m2m:sgn": {
                                "sur": "/id-shore/shore/anchor/commands",
                                "nev": {"net": 3, "rep": {"m2m:cin": {"con": json.dumps(command)}}}}})
                        except Exception as exc:
                            # Failure is observable; the persistent command ledger remains authoritative.
                            engine.errors.append({"at": time.time(), "error": f"PCH hint: {exc}"})
                    threading.Thread(target=hint, daemon=True).start()
                    return self.send(201, command)
                self.send(404, {"error": "Not found"})
            except (ValueError, KeyError) as exc:
                self.send(400, {"error": str(exc)})
            except Exception as exc:
                self.send(502, {"error": str(exc)})

    def simulate_link():
        while not engine.done.wait(2):
            if not engine.bootstrapped:
                continue
            scenario = read_json(RUNTIME / "scenario.json", {})
            if not scenario or scenario.get("enabled") is False:
                continue
            zone = scenario["zone"]
            state = {"zone": zone, "coastalAvailable": zone == "coastal", "satelliteAvailable": zone != "shadow",
                     "rssi": scenario.get("rssi", -70 if zone == "coastal" else -120), "observedAt": time.time(),
                     "source": "local-link-simulator", "scenarioId": scenario.get("scenarioId", "manual")}
            try:
                # Core-only link fixture; no MEC APIs or IPE are involved.
                write_state(Client("ship"), "ship/anchor/linkState", state)
            except Exception as exc:
                engine.errors.append({"at": time.time(), "error": f"Link simulator: {exc}"})

    httpd = ThreadingHTTPServer((BIND_HOST, API_PORT), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(server_cert(), str(RUNTIME / "certs" / "localhost.key"))
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    for target in (() if ROLE == "shore" else (engine.run, engine.commands, engine.poll_loop, simulate_link)):
        threading.Thread(target=target, daemon=True).start()
    # Restart automatically resumes against persisted CSE resources without recreating them.
    try:
        node = "shore" if ROLE == "shore" else "ship"
        Client(node).get(f"{node}/anchor/commands")
        engine.bootstrapped = True
    except Exception:
        pass

    def stop(*_):
        engine.done.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    httpd.serve_forever(poll_interval=0.2)
    httpd.server_close()


if __name__ == "__main__":
    serve()
