from __future__ import annotations

import argparse
import json
import time
import uuid

import requests

from .client import Client, MQTTClient
from .config import API_HOST, API_PORT, LOCAL_NODES, ROLE, RUNTIME, SENSORS, cert
from .models import make_event, record_name


def api(path, body=None):
    with requests.Session() as session:
        session.trust_env = False
        headers = {"Authorization": "Bearer " + (RUNTIME / "api-token").read_text().strip()}
        response = session.request("GET" if body is None else "POST", f"https://{API_HOST}:{API_PORT}{path}",
                                   json=body, headers=headers, verify=cert(), timeout=90)
        if response.status_code >= 400:
            raise RuntimeError(f"API {response.status_code}: {response.text}")
        return response.json()


def emit(count=5, binding="mqtt", sensor="sensor-01", boot=None, start=0, priority="normal", temperature=20.0):
    boot = boot or uuid.uuid4().hex[:12]
    origin = "C" + sensor.replace("-", "")
    client = MQTTClient("ship", origin) if binding == "mqtt" else Client("ship", origin)
    events = []
    try:
        for seq in range(start, start + count):
            event = make_event(sensor, boot, seq, temperature + seq / 10, priority)
            body = {"m2m:cin": {"rn": record_name(event["eventId"]), "con": json.dumps(event), "cnf": "application/json:0"}}
            client.create(f"ship/{sensor}/history", 4, body)
            events.append(event)
    finally:
        if isinstance(client, MQTTClient):
            client.close()
    return events


def main(argv=None):
    parser = argparse.ArgumentParser(description="ANCHOR core. Select role: anchor --role ship|shore|dev COMMAND")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("up", "down", "prepare", "bootstrap", "status"):
        sub.add_parser(name)
    scenario = sub.add_parser("scenario")
    scenario.add_argument("zone", choices=["coastal", "offshore", "shadow"])
    emitter = sub.add_parser("emit")
    emitter.add_argument("--count", type=int, default=5)
    emitter.add_argument("--binding", choices=["mqtt", "http"], default="mqtt")
    emitter.add_argument("--sensor", choices=SENSORS, default=SENSORS[0])
    emitter.add_argument("--priority", choices=["normal", "critical"], default="normal")
    emitter.add_argument("--temperature", type=float, default=20.0)
    command = sub.add_parser("command")
    command.add_argument("target", choices=["fan", "buzzer", "safetyGroup"])
    command.add_argument("state", choices=["on", "off"])
    command.add_argument("--ttl", type=float, default=60)
    restart = sub.add_parser("restart")
    restart.add_argument("node", choices=[*LOCAL_NODES, "service"])
    trust = sub.add_parser("trust-shore", help="Trust the shore Pi's public certificate (ship only)")
    trust.add_argument("certificate")
    connect = sub.add_parser("connect", help="Start SSH forwards to the shore Pi (ship only)")
    connect.add_argument("ssh_target", help="SSH alias or user@host with key authentication configured")
    sub.add_parser("disconnect", help="Stop only the owned SSH tunnel")
    sub.add_parser("info", help="Show role, owned processes and public certificate location")
    args = parser.parse_args(argv)
    if ROLE == "shore" and args.command in ("emit", "scenario"):
        parser.error(f"{args.command} belongs to the ship role")
    if ROLE == "ship" and args.command == "command":
        parser.error("Submit commands using anchor --role shore command on the shore Pi")
    if args.command in ("up", "down", "prepare"):
        from . import stack
        getattr(stack, args.command)()
        result = {"ok": True, "runtime": str(RUNTIME)}
    elif args.command == "bootstrap":
        result = api("/bootstrap", {})
    elif args.command == "status":
        result = api("/status")
    elif args.command == "scenario":
        result = api("/scenario", {"zone": args.zone})
    elif args.command == "emit":
        result = {"events": emit(args.count, args.binding, args.sensor, priority=args.priority, temperature=args.temperature)}
    elif args.command == "command":
        result = api("/commands", {"target": args.target, "desiredState": args.state == "on", "ttl": args.ttl})
    elif args.command == "restart":
        from . import stack
        stack.stop_node(args.node)
        if args.node == "service":
            stack.start_service()
        else:
            stack.start_node(args.node)
        result = {"restarted": args.node}
    elif args.command in ("trust-shore", "connect", "disconnect", "info"):
        from . import deployment
        if args.command == "trust-shore":
            result = deployment.trust_shore(args.certificate)
        elif args.command == "connect":
            result = deployment.connect(args.ssh_target)
        elif args.command == "disconnect":
            result = deployment.disconnect()
        else:
            result = deployment.info()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
