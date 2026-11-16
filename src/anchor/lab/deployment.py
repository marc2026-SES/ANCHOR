"""Two-Pi deployment helpers; no ACME implementation imports or extensions."""
import hashlib
import os
import re
import shutil
import ssl
import subprocess
import time
from pathlib import Path

from .config import API_PORT, DB_PORT, LOCAL_NODES, NODES, ROLE, RUNTIME, read_json, url


def ship_only():
    if ROLE != "ship":
        raise ValueError("This command requires --role ship")


def trust_shore(filename):
    ship_only()
    local = RUNTIME / "certs" / "localhost.crt"
    if not local.exists():
        raise ValueError("Run anchor --role ship prepare or up first")
    source = Path(filename).read_text()
    if "PRIVATE KEY" in source or source.count("-----BEGIN CERTIFICATE-----") != 1:
        raise ValueError("Supply only the shore Pi's public certificate, never its private key")
    # Parse through the public ssl API, then install a local trust bundle.
    der = ssl.PEM_cert_to_DER_cert(source)
    check = subprocess.run(["openssl", "x509", "-noout", "-checkend", "0"],
                           input=source, text=True, capture_output=True)
    if check.returncode:
        raise ValueError("Shore certificate is invalid or expired")
    peer = RUNTIME / "certs" / "shore.crt"
    peer.write_text(source.strip() + "\n")
    bundle = RUNTIME / "certs" / "trust-bundle.pem"
    bundle.write_text(local.read_text().strip() + "\n" + source.strip() + "\n")
    return {"trustedShoreSha256": hashlib.sha256(der).hexdigest(),
            "restartRequired": "anchor --role ship restart service"}


def connect(target):
    ship_only()
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@:\-]*", target):
        raise ValueError("Use an SSH alias or user@host; configure other options in ~/.ssh/config")
    if not (RUNTIME / "certs" / "shore.crt").exists():
        raise ValueError("Import the shore public certificate with trust-shore first")
    executable = shutil.which("ssh")
    if not executable:
        raise RuntimeError("OpenSSH client is required")
    from .stack import alive, spawn, stop_node
    args = [executable, "-N", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=8",
            "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
            "-L", f"127.0.0.1:{NODES['shore']['port']}:127.0.0.1:8700",
            "-L", f"127.0.0.1:{NODES['edge']['port']}:127.0.0.1:8701", target]
    existing = read_json(RUNTIME / "processes.json", {}).get("tunnel")
    if existing and alive(existing['pid']) and existing['args'] != args:
        raise RuntimeError("A different tunnel is running; disconnect it before changing target")
    pid = spawn("tunnel", args)
    from .client import Client
    deadline = time.monotonic() + 12
    last = ""
    while time.monotonic() < deadline:
        try:
            Client("shore", timeout=1).get("shore/anchor/events")
            Client("edge", timeout=1).get("/id-shore/shore/anchor/events")
            return {"connected": True, "pid": pid, "target": target}
        except Exception as exc:
            last = str(exc)
        time.sleep(0.3)
    stop_node("tunnel")
    raise RuntimeError(f"Shore APIs did not become reachable. Check shore bootstrap, SSH key/known_hosts, "
                       f"certificate and {RUNTIME / 'tunnel.log'}. Last error: {last}")


def disconnect():
    ship_only()
    from .stack import stop_node
    stop_node("tunnel")
    return {"disconnected": True, "localCollectionContinues": True}


def info():
    from .stack import alive
    return {"role": ROLE, "localCSEs": list(LOCAL_NODES), "runtime": str(RUNTIME),
            "api": f"https://localhost:{API_PORT}", "databasePort": DB_PORT,
            "cseEndpoints": {n: url(n) for n in NODES},
            "publicCertificate": str(RUNTIME / "certs" / "localhost.crt"),
            "processes": {n: {"pid": v['pid'], "running": alive(v['pid'])}
                          for n, v in read_json(RUNTIME / "processes.json", {}).items()}}
