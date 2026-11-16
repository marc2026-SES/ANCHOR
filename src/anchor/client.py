from __future__ import annotations

import json
import threading
import time
import uuid
from urllib.parse import quote

import requests

from .config import BROKER_PORT, CORE, NODES, cert, url


class OneM2MError(RuntimeError):
    def __init__(self, rsc: int, body: object, target: str):
        self.rsc, self.body, self.target = rsc, body, target
        super().__init__(f"oneM2M {rsc} at {target}: {body}")


def resource(body: dict) -> dict:
    return next(iter(body.values()))


class Client:
    def __init__(self, node="ship", origin=CORE, timeout=8):
        self.node, self.origin, self.timeout = node, origin, timeout
        self.base = url(node)
        self._local = threading.local()

    @property
    def session(self):
        # Engine workers share a Client, but never a mutable requests Session.
        if not hasattr(self._local, "session"):
            self._local.session = requests.Session()
            self._local.session.trust_env = False
        return self._local.session

    def request(self, method, target, body=None, ty=None, params=None, headers=None):
        # HTTP /~/ encodes a oneM2M SP-relative target; preserve it through transit CSEs.
        path = "/~" + target if target.startswith("/id-") else "/" + target.lstrip("/")
        h = {"X-M2M-Origin": self.origin, "X-M2M-RI": uuid.uuid4().hex,
             "X-M2M-RVI": "4", "Accept": "application/json"}
        if body is not None:
            h["Content-Type"] = "application/json" + (f";ty={ty}" if ty else "")
        h.update(headers or {})
        response = self.session.request(method, self.base + quote(path, safe="/~:"),
                                        json=body, params=params, headers=h,
                                        timeout=self.timeout, verify=cert())
        try:
            data = response.json() if response.content else {}
        except ValueError:
            data = {"error": response.text[:500]}
        rsc = int(response.headers.get("X-M2M-RSC", 5000))
        if not (2000 <= rsc < 3000):
            raise OneM2MError(rsc, data, target)
        return data

    def get(self, target, **kw):
        return self.request("GET", target, **kw)

    def create(self, target, ty, body):
        return self.request("POST", target, body, ty)

    def update(self, target, body):
        return self.request("PUT", target, body)

    def notify(self, target, body):
        return self.request("POST", target, body)

    def delete(self, target):
        return self.request("DELETE", target)

    def ensure(self, parent, ty, body):
        name = resource(body)["rn"]
        try:
            existing = self.get(f"{parent}/{name}")
            if resource(existing).get("ty") != ty:
                raise ValueError(f"Existing resource type differs: {parent}/{name}")
            return existing
        except OneM2MError as e:
            if e.rsc != 4004:
                raise
        try:
            return self.create(parent, ty, body)
        except OneM2MError as e:
            if e.rsc != 4105:
                raise
            return self.get(f"{parent}/{name}")

    def children(self, parent):
        """Page through all child references; filter by type after retrieval.

        The pinned server slices before filtering and omits virtual resources.
        A short page therefore does not prove that discovery is complete.
        """
        result, seen, offset, limit = [], set(), 1, 100
        while True:
            page = self.get(parent, params={"fu": 1, "lvl": 1,
                                           "drt": 1, "lim": limit, "ofst": offset})
            refs = page.get("m2m:uril", [])
            fresh = [ref for ref in refs if ref not in seen]
            if not fresh:
                # Pinned ACME may repeat the full result at an exhausted offset.
                # records() independently compares against the CNT's cni.
                return result
            result.extend(fresh)
            seen.update(fresh)
            offset += limit
            if offset > 1_000_000:
                raise RuntimeError(f"Discovery did not converge at {parent}")

    def records(self, parent):
        records = []
        expected = resource(self.get(parent)).get('cni', 0)
        for ref in self.children(parent):
            item = resource(self.get(ref))
            if item['ty'] != 4:
                continue
            con = item["con"]
            records.append((item, json.loads(con) if isinstance(con, str) else con))
        if len(records) < expected:
            raise RuntimeError(f"Incomplete discovery at {parent}: expected at least {expected}, got {len(records)}")
        return records

    def put_record(self, parent, name, value):
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
        try:
            self.create(parent, 4, {"m2m:cin": {"rn": name, "cnf": "application/json:0", "con": payload}})
            return True
        except OneM2MError as e:
            if e.rsc != 4105:
                raise
            existing = resource(self.get(f"{parent}/{name}"))["con"]
            if json.loads(existing) != value:
                raise ValueError(f"Conflicting immutable record: {parent}/{name}")
            return False


class MQTTClient:
    """Actual oneM2M MQTT binding, not a private telemetry topic bridge."""
    def __init__(self, node, origin):
        import paho.mqtt.client as mqtt
        self.node, self.origin = node, origin
        self.pending = {}
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"anchor-{uuid.uuid4().hex}")
        self.client.tls_set(ca_certs=cert())
        self.client.on_connect = self._connect
        self.client.on_subscribe = lambda *args: self.ready.set()
        self.client.on_message = self._message
        self.client.connect(NODES[node].get("mqttHost", "localhost"), NODES[node].get("mqttPort", BROKER_PORT))
        self.client.loop_start()
        if not self.ready.wait(6):
            self.close()
            raise TimeoutError("MQTTS subscription did not become ready")

    def _connect(self, client, userdata, flags, reason, properties):
        client.subscribe(f"/oneM2M/resp/{self.origin}/+/json", qos=1)

    def _message(self, client, userdata, message):
        data = json.loads(message.payload)
        with self.lock:
            if (entry := self.pending.get(data.get("rqi"))) is not None:
                entry[1].update(data)
                entry[0].set()

    def create(self, target, ty, body):
        rqi = uuid.uuid4().hex
        event, response = threading.Event(), {}
        with self.lock:
            self.pending[rqi] = (event, response)
        payload = {"op": 1, "to": target, "fr": self.origin, "rqi": rqi, "rvi": "4", "ty": ty, "pc": body}
        receiver = NODES[self.node]["csi"].lstrip("/").replace("/", ":")
        self.client.publish(f"/oneM2M/req/{self.origin}/{receiver}/json", json.dumps(payload), qos=1)
        try:
            if not event.wait(8):
                raise TimeoutError(f"MQTTS request timed out: {target}")
            if not 2000 <= response.get("rsc", 5000) < 3000:
                raise OneM2MError(response.get("rsc", 5000), response, target)
            return response.get("pc", {})
        finally:
            with self.lock:
                self.pending.pop(rqi, None)

    def close(self):
        self.client.disconnect()
        self.client.loop_stop()
