from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass


def record_name(event_id: str) -> str:
    return "e-" + hashlib.sha256(event_id.encode()).hexdigest()[:32]


def validate_event(event: dict):
    for key in ("vesselId", "deviceId", "bootId"):
        if not isinstance(event.get(key), str) or not event[key] or ":" in event[key]:
            raise ValueError(f"Invalid {key}")
    if type(event.get("seq")) is not int or event["seq"] < 0:
        raise ValueError("seq must be a nonnegative integer")
    expected = ":".join(str(event[k]) for k in ("vesselId", "deviceId", "bootId", "seq"))
    if event.get("eventId") != expected:
        raise ValueError("eventId does not match stream identity")
    if not isinstance(event.get("generatedAt"), (int, float)) or not math.isfinite(event["generatedAt"]):
        raise ValueError("Invalid generatedAt")
    if event.get("priority") not in ("normal", "critical"):
        raise ValueError("Invalid priority")
    measurement = event.get("measurement", {})
    value = measurement.get("curT0") if isinstance(measurement, dict) else None
    if (not isinstance(measurement, dict) or event.get("schemaVersion") != 1 or
        measurement.get("cnd") != "org.onem2m.common.moduleclass.temperature" or
        type(value) not in (int, float) or not math.isfinite(value)):
        raise ValueError("Invalid temperature measurement schema")


def make_event(device, boot, seq, value=20.0, priority="normal"):
    event = {"schemaVersion": 1, "vesselId": "ship-001", "deviceId": device,
             "bootId": boot, "seq": seq, "generatedAt": time.time(), "priority": priority,
             "measurement": {"cnd": "org.onem2m.common.moduleclass.temperature", "curT0": value}}
    event["eventId"] = ":".join(str(event[k]) for k in ("vesselId", "deviceId", "bootId", "seq"))
    validate_event(event)
    return event


def reconcile(source: list[dict], received: list[dict]) -> dict:
    src = {v["eventId"]: v for v in source}
    dst = {v["eventId"]: v for v in received}
    conflicts = sorted(k for k in src.keys() & dst.keys() if src[k] != dst[k])
    pending = sorted(src.keys() - dst.keys())
    streams = {}
    for event in source:
        key = ":".join(str(event[k]) for k in ("vesselId", "deviceId", "bootId"))
        streams.setdefault(key, []).append(event)
    checkpoints = {}
    for key, events in streams.items():
        seqs = {e["seq"] for e in events if e["eventId"] in dst and e["eventId"] not in conflicts}
        highest = -1
        while highest + 1 in seqs:
            highest += 1
        checkpoints[key] = highest
    return {"sourceUnique": len(src), "shoreUnique": len(dst), "pending": len(pending),
            "missingEventIds": pending, "unexpectedEventIds": sorted(dst.keys() - src.keys()),
            "conflictingEventIds": conflicts, "checkpoints": checkpoints,
            "complete": not pending and not conflicts}


@dataclass
class RoutePolicy:
    hold_seconds: float = 2.0
    dwell_seconds: float = 5.0
    max_age: float = 10.0
    candidate: str | None = None
    candidate_since: float = 0
    last_change: float = 0

    def choose(self, link: dict, active: str | None, now=None) -> str | None:
        now = time.time() if now is None else now
        if now - link.get("observedAt", 0) > self.max_age or link.get("observedAt", 0) > now + 2:
            desired = None
        elif link.get("coastalAvailable") and link.get("zone") == "coastal" and link.get("rssi", -200) >= (-110 if active == "coastal" else -100):
            desired = "coastal"
        elif link.get("satelliteAvailable"):
            desired = "offshore"
        else:
            desired = None
        if desired is None:
            self.candidate = None
            return None
        if desired == active:
            self.candidate = desired
            return active
        if desired != self.candidate:
            self.candidate, self.candidate_since = desired, now
        if now - self.candidate_since < self.hold_seconds:
            return active
        if active and now - self.last_change < self.dwell_seconds:
            return active
        self.last_change = now
        return desired
