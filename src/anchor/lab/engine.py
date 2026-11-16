from __future__ import annotations

import json
from collections import deque
import threading
import time
import uuid

from .client import Client, OneM2MError, resource
from .config import ACTUATORS, COMMAND, SENSORS
from .models import RoutePolicy, reconcile, record_name, validate_event
from .resources import latest, set_batch, write_state


class Engine:
    def __init__(self):
        self.ship = Client("ship")
        self.uplinks = {node: Client(node) for node in ("edge", "shore")}
        self.active = None
        self.policy = RoutePolicy()
        self.state = {"phase": "STARTING", "route": None}
        self.metrics = {"pending": None, "complete": False}
        self.done = threading.Event()
        self.wake = threading.Event()
        self.bootstrapped = False
        self.source = {}
        self.received = {}
        self.last_scan = 0
        self.last_metrics = 0
        self.notifications = 0
        self.notification_batches = 0
        self.handover_count = 0
        self.replay_count = 0
        self.errors = deque(maxlen=32)
        self.command_wake = threading.Event()
        self.pch_received = 0
        self.last_measurements = {}
        self._lock = threading.RLock()
        self._command_lock = threading.RLock()

    def status(self):
        with self._lock:
            return {"state": dict(self.state), "metrics": dict(self.metrics),
                    "notificationRequests": self.notifications, "batchNotifications": self.notification_batches,
                    "pollingChannelReceived": self.pch_received,
                    "routeTransitionCount": self.handover_count, "replayed": self.replay_count,
                    "errors": list(self.errors)[-8:], "source": "ACME oneM2M resources"}

    def notify(self, body):
        with self._lock:
            self.notifications += 1
            if "m2m:agn" in body:
                self.notification_batches += 1
        # Treat notifications as hints. Source records are always recoverable by discovery.
        self.last_scan = 0
        self.wake.set()

    def state_event(self, phase, **fields):
        with self._lock:
            self.state = {**self.state, "phase": phase, "updatedAt": time.time(), **fields}
        write_state(self.ship, "ship/anchor/handoverState", self.state)

    def uplink(self):
        """Application uplink, not dynamic CSE re-registration."""
        return self.uplinks["edge" if self.active == "coastal" else "shore"]

    def change_route(self, route):
        started = time.monotonic()
        previous = self.active
        self.state_event("VERIFYING", transitionId=uuid.uuid4().hex,
                         previousRoute=previous, targetRoute=route, error=None,
                         mechanism="application-uplink-selection")
        if route:
            candidate = "edge" if route == "coastal" else "shore"
            # Coastal: API request enters edge and follows its fixed Mcc registration.
            # Offshore: application calls shore directly. Ship CSE remains independent.
            Client(candidate).get("/id-shore/shore/anchor/events")
            try:
                Client(candidate, "CAnchorDashboard").get("/id-shore/shore/anchor/location")
            except OneM2MError as exc:
                if exc.rsc not in (4103, 4101):
                    raise
            else:
                raise RuntimeError("Location privacy check failed")
            set_batch(route)
        self.active = route
        self.last_scan = 0
        self.handover_count += 1
        self.state_event("REPLAYING" if route else "DISCONNECTED", route=route,
                         transitionSeconds=time.monotonic() - started)

    def scan_source(self):
        source = {}
        for sensor in SENSORS:
            for _, event in self.ship.records(f"ship/{sensor}/history"):
                validate_event(event)
                if event["deviceId"] != sensor:
                    raise ValueError("Event device identity does not match its owning resource")
                if event["eventId"] in source and source[event["eventId"]] != event:
                    raise ValueError("Source identity collision")
                source[event["eventId"]] = event
        # Detect retention loss while this process is running; never silently hide it.
        disappeared = set(self.source) - set(source) - set(self.received)
        if disappeared:
            raise RuntimeError(f"Unacknowledged source records expired: {sorted(disappeared)}")
        self.source = source
        for sensor in SENSORS:
            events = [e for e in source.values() if e['deviceId'] == sensor]
            if events:
                newest = max(events, key=lambda e: (e['generatedAt'], e['eventId']))
                if self.last_measurements.get(sensor) != newest['eventId']:
                    self.ship.update(f"ship/{sensor}/measurement", {
                        "cod:tempe": {"curT0": float(newest['measurement']['curT0'])}})
                    self.last_measurements[sensor] = newest['eventId']

    def local_safety(self):
        # Demonstration temperature rule, independent of the shore connection.
        # The group service performs fan-out; physical drivers are still simulated.
        for event in self.source.values():
            if event['measurement']['curT0'] >= 60:
                self.process_command({"commandId": "local-" + record_name(event['eventId']),
                                      "target": "safetyGroup", "desiredState": True,
                                      "issuedAt": event['generatedAt'],
                                      "expiresAt": event['generatedAt'] + 60})

    def scan_shore(self):
        values = self.uplink().records("/id-shore/shore/anchor/events")
        self.received = {event["eventId"]: event for _, event in values}

    def deliver(self):
        pending = [v for k, v in self.source.items() if k not in self.received]
        pending.sort(key=lambda v: (v["priority"] != "critical", v["deviceId"], v["bootId"], v["seq"]))
        for event in pending[:100]:
            if self.done.is_set():
                return
            # Deterministic rn + content comparison makes replay idempotent even if ACK is lost.
            self.uplink().put_record("/id-shore/shore/anchor/events", record_name(event["eventId"]), event)
            self.received[event["eventId"]] = event
            self.replay_count += 1

    def tick(self):
        # Local acquisition and safety must still run when the remote CSE fails.
        now = time.monotonic()
        scan_due = now - self.last_scan > 5
        if scan_due:
            self.scan_source()
            self.last_scan = now
        self.local_safety()
        link = latest(self.ship, "ship/anchor/linkState", {})
        desired = self.policy.choose(link, self.active)
        if desired != self.active or self.state["phase"] == "STARTING":
            self.change_route(desired)
        if self.active and (scan_due or self.last_scan == 0):
            self.scan_shore()
        if self.active:
            self.deliver()
        metrics = reconcile(list(self.source.values()), list(self.received.values()))
        metrics.update(updatedAt=time.time(), shoreVerified=bool(self.active), route=self.active,
                       guarantee="CSE-accepted records within configured retention; see README.md")
        if not self.active:
            metrics["complete"] = False
        self.metrics = metrics
        if now - self.last_metrics > 3:
            write_state(self.ship, "ship/anchor/deliveryMetrics", metrics)
            if self.active:
                write_state(self.uplink(), "/id-shore/shore/anchor/deliveryMetrics", metrics)
                write_state(self.ship, "ship/anchor/checkpoints", metrics["checkpoints"])
            self.last_metrics = now
        if self.active and metrics["complete"] and self.state["phase"] != "ACTIVE":
            self.state_event("ACTIVE", route=self.active, recoveryCompletedAt=time.time())

    def run(self):
        while not self.done.is_set():
            if self.bootstrapped:
                try:
                    self.tick()
                except Exception as exc:
                    with self._lock:
                        self.errors.append({"at": time.time(), "error": str(exc)})
                        self.state = {**self.state, "phase": "DEGRADED", "error": str(exc)}
                    # Route must be reverified after a fault; no false ACTIVE status.
                    self.active = None
                    self.state["route"] = None
                    self.metrics = {**self.metrics, "complete": False, "shoreVerified": False, "route": None}
                    self.last_scan = 0
                    self.done.wait(2)
            self.wake.wait(0.5)
            self.wake.clear()

    def process_command(self, command):
        with self._command_lock:
            return self._process_command(command)

    def _process_command(self, command):
        command_id = command["commandId"]
        name = record_name(command_id)
        results = "ship/anchor/commandResults"
        try:
            return json.loads(resource(self.ship.get(f"{results}/{name}"))["con"])
        except OneM2MError as exc:
            if exc.rsc != 4004:
                raise
        status = "expired" if time.time() >= command["expiresAt"] else "executed"
        if status == "executed":
            target = command["target"]
            if target not in (*ACTUATORS, "safetyGroup") or type(command["desiredState"]) is not bool:
                raise ValueError("Unsupported actuator command")
            parent = "ship/anchor/safetyGroup/fopt" if target == "safetyGroup" else f"ship/{target}/commands"
            body = {"m2m:cin": {"rn": name, "con": json.dumps(command), "cnf": "application/json:0"}}
            if target == "safetyGroup":
                response = self.ship.create(parent, 4, body)
                responses = response.get("m2m:agr", {}).get("m2m:rsp", [])
                if not responses or any(r["rsc"] not in (2001, 4105) for r in responses):
                    raise RuntimeError(f"Group member failed: {response}")
                # A 4105 is safe only if the existing immutable member command matches.
                for actuator in ACTUATORS:
                    stored = json.loads(resource(self.ship.get(f"ship/{actuator}/commands/{name}"))["con"])
                    if stored != command:
                        raise ValueError(f"Conflicting group command on {actuator}")
            else:
                self.ship.put_record(parent, name, command)
            # Simulated actuator driver, explicitly labelled. Replace with ESP32 consumer.
            for actuator in ACTUATORS if target == "safetyGroup" else [target]:
                self.ship.update(f"ship/{actuator}/reportedState", {"cod:binSh": {"state": command["desiredState"]}})
                self.ship.put_record(f"ship/{actuator}/executions", name, {"commandId": command_id, "state": command["desiredState"], "driver": "simulator"})
        result = {"commandId": command_id, "status": status, "driver": "simulator"}
        self.ship.put_record(results, name, result)
        return result

    def poll_once(self):
        if not self.active:
            return
        client = Client("shore", COMMAND, timeout=7)
        try:
            content = client.get("shore/shipCommands/pch/pcu", headers={"X-M2M-RET": "2000"})
        except OneM2MError as exc:
            if exc.rsc in (4008, 5103):
                return
            raise
        requests = content.get("m2m:agrp", [content])
        for request in requests:
            request = request.get("m2m:rqp", request)
            # Acknowledge receipt; durable command completion is a separate resource.
            client.notify("shore/shipCommands/pch/pcu", {"m2m:rsp": {
                "rqi": request["rqi"], "rsc": 2000, "fr": COMMAND, "to": request["fr"], "rvi": "4"}})
            self.pch_received += 1
            self.command_wake.set()

    def commands(self):
        while not self.done.is_set():
            self.command_wake.wait(1)
            self.command_wake.clear()
            if self.done.is_set():
                break
            if not self.bootstrapped or not self.active:
                continue
            try:
                # Ledger is authoritative even when a PCH hint expired or was lost in a restart.
                commands = self.uplink().records("/id-shore/shore/anchor/commands")
                for _, command in sorted(commands, key=lambda pair: (pair[1]["issuedAt"], pair[1]["commandId"])):
                    result = self.process_command(command)
                    self.uplink().put_record("/id-shore/shore/anchor/commandResults", record_name(command["commandId"]), result)
            except Exception as exc:
                self.errors.append({"at": time.time(), "error": f"command reconciliation: {exc}"})

    def poll_loop(self):
        while not self.done.wait(0.2):
            if not self.bootstrapped or not self.active:
                continue
            try:
                self.poll_once()
            except Exception as exc:
                self.errors.append({"at": time.time(), "error": f"PCH: {exc}"})
