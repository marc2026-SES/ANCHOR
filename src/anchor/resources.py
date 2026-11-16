from __future__ import annotations

import json
import time

from .client import Client, OneM2MError, resource
from .config import ACTUATORS, API_HOST, API_PORT, COMMAND, CORE, DASHBOARD, LOCAL_NODES, NODES, OWNER, SENSORS


def ae(client, name, origin, reachable=True, poa=None):
    try:
        existing = resource(client.get(f"{client.node}/{name}"))
        if existing.get("aei") != origin:
            raise ValueError(f"AE {name} belongs to another originator")
        return existing
    except OneM2MError as exc:
        if exc.rsc != 4004:
            raise
    body = {"rn": name, "api": "Nanchor.v1", "rr": reachable, "srv": ["4"]}
    if poa:
        body["poa"] = poa
    return resource(Client(client.node, origin).ensure(client.node, 2, {"m2m:ae": body}))


def acp(client, name, grants):
    # Transit CSEs qualify a CSE-relative originator with their CSE-ID.
    # Grant only these known identities, never a wildcard originator.
    def qualified(origins):
        return list(dict.fromkeys(o for origin in origins
                                 for o in (origin, *(n['csi'] + '/' + origin for n in NODES.values()))))
    body = {"m2m:acp": {"rn": name,
        "pv": {"acr": [{"acor": qualified(origins), "acop": ops} for origins, ops in grants]},
        "pvs": {"acr": [{"acor": ["CAdmin"], "acop": 63}]}}}
    created = resource(client.ensure(client.node, 1, body))
    # Reconcile existing rules so a second bootstrap applies permission changes.
    client.update(f"{client.node}/{name}", {"m2m:acp": {"pv": body["m2m:acp"]["pv"], "pvs": body["m2m:acp"]["pvs"]}})
    return created["ri"]


def container(client, parent, name, policy=None, limit=100000):
    fields = {"rn": name, "mni": limit, "mbs": 256_000_000, "mia": 604800}
    if policy:
        fields["acpi"] = [policy]
    return client.ensure(parent, 3, {"m2m:cnt": fields})


def assign_policy(client, path, policy, owner):
    current = resource(client.get(path))
    if current.get("acpi") == [policy]:
        return
    # oneM2M requires acpi to be the only attribute in an UPDATE. Initial
    # assignment on an AE without acpi must be made by that AE's creator.
    actor = Client(client.node, "CAdmin" if current.get("acpi") else owner)
    actor.update(path, {"m2m:ae": {"acpi": [policy]}})


def write_state(client, parent, value):
    # State history lives in the CSE. No second application telemetry database.
    client.create(parent, 4, {"m2m:cin": {"con": json.dumps(value, separators=(",", ":")), "cnf": "application/json:0"}})


def latest(client, parent, default=None):
    try:
        value = resource(client.get(parent + "/la"))["con"]
        return json.loads(value) if isinstance(value, str) else value
    except OneM2MError as exc:
        if exc.rsc == 4004:
            return default
        raise


def bootstrap():
    ids = {}
    for node in LOCAL_NODES:
        c = Client(node, "CAdmin")
        ae(c, "anchor", CORE)
        ae(c, "dashboard", DASHBOARD)
        ae(c, "owner", OWNER)
        public = acp(c, "anchor-public", [([CORE], 63), ([DASHBOARD, OWNER], 34)])
        private = acp(c, "anchor-private", [([CORE], 63), ([OWNER], 34)])
        assign_policy(c, f"{node}/anchor", public, CORE)
        for name in ("handoverState", "deliveryMetrics", "checkpoints", "events", "commands", "commandResults"):
            container(c, f"{node}/anchor", name, public, 100000 if name in ("events", "commands", "commandResults") else 200)
        for name in ("linkState", "location"):
            container(c, f"{node}/anchor", name, private, 200)
        ids[node] = {"publicACP": public, "privateACP": private}
    if "ship" in LOCAL_NODES:
        bootstrap_ship(ids)
    if "shore" in LOCAL_NODES:
        bootstrap_shore()
    return ids


def bootstrap_ship(ids):
    ship = Client("ship", "CAdmin")
    for sensor in SENSORS:
        origin = "C" + sensor.replace("-", "")
        sensor_ae = ae(ship, sensor, origin)
        policy = acp(ship, f"policy-{sensor}", [([CORE], 63), ([origin], 7), ([DASHBOARD, OWNER], 34)])
        assign_policy(ship, f"ship/{sensor}", policy, origin)
        ship.update(f"ship/{sensor}", {"m2m:ae": {"lbl": ["vessel:ship-001", "role:sensor", "measurement:temperature"]}})
        container(ship, f"ship/{sensor}", "history", policy)
        ship.ensure(f"ship/{sensor}", 28, {"cod:tempe": {
            "rn": "measurement", "cnd": "org.onem2m.common.moduleclass.temperature", "curT0": 20.0,
            "lbl": ["vessel:ship-001", "measurement:temperature"], "acpi": [policy]}})
        subscriber = Client("ship", CORE)
        sub_path = f"ship/{sensor}/history/delivery"
        try:
            subscriber.update(sub_path, {"m2m:sub": {"nct": 1}})
        except OneM2MError as exc:
            if exc.rsc == 4103:
                ship.delete(sub_path)  # Migrate a prior bootstrap-owned subscription.
            elif exc.rsc != 4004:
                raise
        subscriber.ensure(f"ship/{sensor}/history", 23, {"m2m:sub": {
            "rn": "delivery", "nu": [f"https://{API_HOST}:{API_PORT}/notify/telemetry"], "enc": {"net": [3]}, "nct": 1}})
    members = []
    for target in ACTUATORS:
        origin = "C" + target
        ae(ship, target, origin)
        policy = acp(ship, f"policy-{target}", [([CORE], 63), ([origin], 7), ([DASHBOARD, OWNER], 34)])
        assign_policy(ship, f"ship/{target}", policy, origin)
        commands = resource(container(ship, f"ship/{target}", "commands", policy))
        container(ship, f"ship/{target}", "executions", policy)
        ship.ensure(f"ship/{target}", 28, {"cod:binSh": {"rn": "reportedState", "cnd": "org.onem2m.common.moduleclass.binarySwitch", "state": False, "acpi": [policy]}})
        members.append(commands["ri"])
    ship.ensure("ship/anchor", 9, {"m2m:grp": {"rn": "safetyGroup", "mt": 3, "mnm": 2, "mid": members,
                                               "acpi": [ids["ship"]["publicACP"]]}})


def bootstrap_shore():
    shore = Client("shore", "CAdmin")
    cmd = ae(shore, "shipCommands", COMMAND, reachable=False)
    cmd_policy = acp(shore, "policy-ship-commands", [([CORE, COMMAND], 63)])
    assign_policy(shore, "shore/shipCommands", cmd_policy, COMMAND)
    Client("shore", COMMAND).ensure("shore/shipCommands", 15, {"m2m:pch": {"rn": "pch", "rqag": True}})


def set_batch(route):
    ship = Client("ship")
    for sensor in SENSORS:
        ship.update(f"ship/{sensor}/history/delivery", {"m2m:sub": {
            "bn": {"num": 10, "dur": "PT2S"} if route == "offshore" else None}})
