from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from .config import ACME, API_PORT, BROKER_PORT, DB_PORT, LOCAL_NODES, NODES, PIN, PROJECT, ROLE, RUNTIME, atomic_json, read_json, url


def prepare():
    if not (ACME / "acmecse").is_dir():
        raise RuntimeError(f"ACME checkout missing: {ACME}")
    revision = subprocess.check_output(["git", "-C", str(ACME), "rev-parse", "HEAD"], text=True).strip()
    if revision != PIN:
        raise RuntimeError(f"ACME revision differs from tested pin {PIN}: {revision}")
    RUNTIME.mkdir(parents=True, exist_ok=True)
    os.chmod(RUNTIME, 0o700)
    certs = RUNTIME / "certs"
    certs.mkdir(exist_ok=True)
    if not (certs / "localhost.crt").exists():
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "90",
                        "-keyout", str(certs / "localhost.key"), "-out", str(certs / "localhost.crt"),
                        "-subj", f"/CN=anchor-{ROLE}", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                       check=True, capture_output=True)
        os.chmod(certs / "localhost.key", 0o600)
    if not (RUNTIME / "api-token").exists():
        (RUNTIME / "api-token").write_text(secrets.token_urlsafe(32))
        os.chmod(RUNTIME / "api-token", 0o600)
    if not (RUNTIME / "db-password").exists():
        (RUNTIME / "db-password").write_text(secrets.token_urlsafe(32))
        os.chmod(RUNTIME / "db-password", 0o600)
    broker = RUNTIME / "broker"
    broker.mkdir(exist_ok=True)
    (broker / "mosquitto.conf").write_text(f"""# Local developer network only. See docs/DEPLOYMENT_PI5.md for device credentials.
listener {BROKER_PORT} 127.0.0.1
allow_anonymous true
certfile {certs / 'localhost.crt'}
keyfile {certs / 'localhost.key'}
persistence false
log_type warning
""")
    for name in LOCAL_NODES:
        node = NODES[name]
        base = RUNTIME / name
        base.mkdir(exist_ok=True)
        # Remove only ANCHOR adapters left by the earlier development profile.
        # ACME now runs unmodified, with no user plugins or Python monkey patches.
        for adapter in ("AnchorTLS.py", "AnchorCompatibility.py", "AnchorRegistrar.py"):
            (base / "plugins" / adapter).unlink(missing_ok=True)
        cache = base / "plugins" / "__pycache__"
        if cache.exists():
            for entry in cache.glob("Anchor*.pyc"):
                entry.unlink()
        registrar = "shore" if name == "edge" else "edge"
        rc = NODES[registrar]
        (base / "acme.ini").write_text(f"""[basic.config]
cseType={node['type']}
cseID={node['csi'][1:]}
cseName={node['rn']}
cseHost=localhost
httpPort={node['port']}
databaseType=postgresql
adminID=CAdmin
networkInterface=127.0.0.1
serviceProviderID=//anchor.local
secret={read_json(RUNTIME / 'secret.json', {}).get(name) or 'anchor-local-' + name}

[cse]
enableRemoteCSE={str(name != "ship").lower()}
releaseVersion=4
supportedReleaseVersions=4
requestExpirationDelta=5
poa={url(name)}

[http]
address={url(name)}
port={node['port']}
listenIF=127.0.0.1

[http.security]
useTLS=true
verifyCertificate=false
caCertificateFile={certs / 'localhost.crt'}
caPrivateKeyFile={certs / 'localhost.key'}

[mqtt]
enable={str(ROLE != "shore").lower()}
address=localhost
port={BROKER_PORT}

[mqtt.security]
useTLS=true
verifyCertificate=true
caCertificateFile={certs / 'localhost.crt'}

[cse.registration]
allowedCSROriginators=/id-ship,/id-edge,/id-shore
checkInterval=2
checkLiveliness=false
unregisterWhenStopping=false

[cse.registrar]
address={url(registrar) if name == 'edge' else ''}
cseID={rc['csi'] if name == 'edge' else '/'}
resourceName={rc['rn'] if name == 'edge' else ''}
INCSEcseID=/id-shore

[database.postgresql]
host=127.0.0.1
port={DB_PORT}
role=anchor
password={(RUNTIME / 'db-password').read_text().strip()}
database=postgres
schema=anchor_{name}

[logging]
level=warn
enableFileLogging=true
enableScreenLogging=false

[textui]
enable=false
[webui]
enable=false
""")
        os.chmod(base / "acme.ini", 0o600)
    atomic_json(RUNTIME / "provenance.json", {"acmeCommit": revision, "python": sys.version, "preparedAt": time.time()})


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def spawn(name, args, cwd=None, env=None):
    state = read_json(RUNTIME / "processes.json", {})
    if name in state and alive(state[name]["pid"]):
        return state[name]["pid"]
    logfile = RUNTIME / f"{name}.log"
    with logfile.open("a") as output:
        process = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
    state[name] = {"pid": process.pid, "args": args, "log": str(logfile)}
    atomic_json(RUNTIME / "processes.json", state)
    return process.pid


def start_node(name):
    if name not in LOCAL_NODES:
        raise ValueError(f"{name} is not hosted by role {ROLE}")
    env = os.environ.copy()
    env["REQUESTS_CA_BUNDLE"] = str(RUNTIME / "certs" / "localhost.crt")
    env["SSL_CERT_FILE"] = env["REQUESTS_CA_BUNDLE"]
    env["PYTHONPATH"] = os.pathsep.join([str(ACME), str(PROJECT / "src")])
    spawn(name, [sys.executable, "-m", "acmecse", "--headless", "--base-directory", str(RUNTIME / name),
                 "--config", "acme.ini"], cwd=str(RUNTIME / name), env=env)
    from .client import Client
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        try:
            Client(name, "CAdmin", timeout=1).get(name)
            return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f"{name} CSE did not start. Inspect {RUNTIME / (name + '.log')}")


def up():
    check_ports()
    prepare()
    start_database()
    if ROLE != "shore":
        broker = shutil.which("mosquitto") or "/opt/homebrew/sbin/mosquitto"
        usable = Path(broker).is_file() and subprocess.run([broker, "-h"], capture_output=True).returncode == 0
        if usable:
            spawn("broker", [broker, "-c", str(RUNTIME / "broker" / "mosquitto.conf")])
        else:
            spawn("broker", [sys.executable, "-m", "anchor.lab.broker"])
        time.sleep(0.3)
    for name in LOCAL_NODES:
        start_node(name)
    start_service()


def start_service():
    from .cli import api
    spawn("service", [sys.executable, "-m", "anchor.lab.service"], cwd=str(PROJECT))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            if api("/health").get("role") == ROLE:
                return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("Core service did not become ready; inspect .runtime/service.log")


def check_ports():
    owned = read_json(RUNTIME / "processes.json", {})
    ports = {**{n: NODES[n]['port'] for n in LOCAL_NODES}, "service": API_PORT, "database": DB_PORT}
    if ROLE != "shore":
        ports['broker'] = BROKER_PORT
    for name, port in ports.items():
        if name in owned and alive(owned[name]['pid']):
            continue
        with socket.socket() as probe:
            try:
                probe.bind(('127.0.0.1', port))
            except OSError as exc:
                raise RuntimeError(f"Port {port} for {name} is already in use. Stop the other profile first; "
                                   f"role {ROLE} will not reuse another process.") from exc


def stop_node(name):
    if name in NODES and name not in LOCAL_NODES:
        raise ValueError(f"{name} is not hosted by role {ROLE}")
    state = read_json(RUNTIME / "processes.json", {})
    entry = state.get(name)
    if not entry:
        return
    pid = entry["pid"]
    if alive(pid):
        # Refuse to signal an unrelated process if a stale PID has been reused.
        cmd = subprocess.check_output(["ps", "-p", str(pid), "-o", "command="], text=True).strip()
        expected = ("ssh",) if name == "tunnel" else ("anchor.lab.service",) if name == "service" else ("postgres",) if name == "database" else ("mosquitto", "anchor.lab.broker") if name == "broker" else ("acmecse",)
        if not any(part in cmd for part in expected) or (name in NODES and str(RUNTIME / name) not in cmd):
            raise RuntimeError(f"PID {pid} no longer belongs to {name}; refusing to stop it")
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 15
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        if alive(pid):
            raise RuntimeError(f"{name} did not stop within 15 seconds; process record retained")
    state.pop(name, None)
    atomic_json(RUNTIME / "processes.json", state)


def down():
    for name in ("service", *reversed(LOCAL_NODES), "broker", "database", "tunnel"):
        stop_node(name)


def start_database():
    """Private dev cluster, used only by ACME's built-in PostgreSQL driver."""
    postgres = postgres_binary("postgres")
    initdb = postgres_binary("initdb")
    if not postgres or not initdb:
        raise RuntimeError("PostgreSQL binaries required: put postgres/initdb/pg_isready on PATH; see README.md")
    directory = RUNTIME / "postgres"
    socket_dir = RUNTIME / "pg-socket"
    socket_dir.mkdir(mode=0o700, exist_ok=True)
    if not (directory / "PG_VERSION").exists():
        subprocess.run([initdb, "-D", str(directory), "-U", "anchor", "--encoding=UTF8", "--no-locale",
                        "--auth-host=scram-sha-256", "--auth-local=trust",
                        "--pwfile", str(RUNTIME / "db-password")], check=True, capture_output=True)
    spawn("database", [postgres, "-D", str(directory), "-h", "127.0.0.1", "-p", str(DB_PORT),
                       "-k", str(socket_dir), "-c", "unix_socket_permissions=0700",
                       "-c", "shared_buffers=128MB", "-c", "max_connections=30"])
    ready = str(Path(postgres).parent / "pg_isready")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if subprocess.run([ready, "-h", "127.0.0.1", "-p", str(DB_PORT), "-d", "postgres", "-U", "anchor"],
                          capture_output=True).returncode == 0:
            return
        time.sleep(0.2)
    raise RuntimeError("Local PostgreSQL did not start; inspect .runtime/database.log")


def postgres_binary(name):
    explicit = os.environ.get("ANCHOR_PG_BIN")
    if explicit:
        path = Path(explicit) / name
        return str(path) if path.is_file() else None
    if found := shutil.which(name):
        return found
    # Debian/Raspberry Pi OS installs versioned server binaries outside PATH.
    candidates = list(Path('/usr/lib/postgresql').glob(f'*/bin/{name}'))
    if candidates:
        return str(max(candidates, key=lambda p: tuple(int(v) for v in p.parts[-3].split('.'))))
    return None
