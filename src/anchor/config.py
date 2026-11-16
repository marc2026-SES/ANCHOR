from __future__ import annotations

import json
import os
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
ROLE = os.environ.get("ANCHOR_ROLE", "dev")
if ROLE not in ("dev", "ship", "shore"):
    raise ValueError("ANCHOR_ROLE must be dev, ship or shore")
LOCAL_NODES = {"dev": ("shore", "edge", "ship"), "ship": ("ship",), "shore": ("shore", "edge")}[ROLE]
RUNTIME = Path(os.environ.get("ANCHOR_RUNTIME", PROJECT / (".runtime" if ROLE == "dev" else f".runtime-{ROLE}"))).resolve()
ACME = Path(os.environ.get("ANCHOR_ACME", PROJECT.parent / "ACME-oneM2M-CSE")).resolve()
PIN = "283bd7d696090e983c07c149511102caab99642e"
CORE = "CAnchorCore"
DASHBOARD = "CAnchorDashboard"
OWNER = "CAnchorOwner"
COMMAND = "CShipCommand"
SENSORS = tuple(f"sensor-{n:02}" for n in range(1, 5))
ACTUATORS = ("fan", "buzzer")
NODES = {
    "shore": {"csi": "/id-shore", "rn": "shore", "port": 8700, "type": "IN"},
    "edge": {"csi": "/id-edge", "rn": "edge", "port": 8701, "type": "MN"},
    "ship": {"csi": "/id-ship", "rn": "ship", "port": 8702, "type": "MN"},
}
BROKER_PORT = 18883
DB_PORT = 15433 if ROLE == "ship" else 15432
API_PORT = 8791 if ROLE == "shore" else 8790
# Peer CSEs remain loopback-only behind the ship's SSH forwards.
if ROLE == "ship":
    NODES["shore"]["port"] = 18700
    NODES["edge"]["port"] = 18701
TOPOLOGY = {}
if os.environ.get("ANCHOR_TOPOLOGY"):
    TOPOLOGY = json.loads(Path(os.environ["ANCHOR_TOPOLOGY"]).read_text())
    for role, values in TOPOLOGY.get("nodes", {}).items():
        if role not in NODES:
            raise ValueError(f"Unknown CSE role: {role}")
        NODES[role].update(values)
API_HOST = TOPOLOGY.get("apiHost", "localhost")
BIND_HOST = TOPOLOGY.get("bindHost", "127.0.0.1")


def url(node: str) -> str:
    return f"https://{NODES[node].get('host', 'localhost')}:{NODES[node]['port']}"


def cert() -> str:
    bundle = RUNTIME / "certs" / "trust-bundle.pem"
    return os.environ.get("ANCHOR_CA", str(bundle if bundle.exists() else RUNTIME / "certs" / "localhost.crt"))


def server_cert() -> str:
    return str(RUNTIME / "certs" / "localhost.crt")


def atomic_json(path: Path, value: object) -> None:
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
        temp = Path(f.name)
    os.replace(temp, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default
