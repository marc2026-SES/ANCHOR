"""Explicit endpoints: the coastal CSE must never be reached through shore."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

ROLES = ("ship", "edge", "shore")
ORIGINS = {role: "CAnchor" + role.title() for role in ROLES}
DEVICES = {"bridge": "CBridge", "engine": "CEngine", "cargo": "CCargo"}
SENSORS = {"bridge": ("attitude", "position"), "engine": ("vibration", "gas"),
           "cargo": ("climate", "hatch")}


@dataclass(frozen=True)
class Endpoint:
    url: str
    root: str
    csi: str
    rvi: str = "3"
    ca: str | bool = True
    timeout: float = 3

    def __post_init__(self):
        if urlparse(self.url).scheme not in ("http", "https"):
            raise ValueError("CSE URL must be http(s)")
        if not self.root or "/" in self.root or not self.csi.startswith("/"):
            raise ValueError("Invalid CSE root or ID")
        if not (self.ca is True or (isinstance(self.ca, str) and self.ca.strip())) or not 0 < self.timeout <= 30:
            raise ValueError("Certificate verification cannot be disabled; timeout must be 0..30")


@dataclass
class Settings:
    role: str
    nodes: dict[str, Endpoint]
    vessel_id: str = "ship-001"
    bind: str = "127.0.0.1"
    port: int = 8790
    notify_url: str = "https://127.0.0.1:8790/notify"
    token_file: str = ""
    status_token_file: str = ""
    tls_cert: str = ""
    tls_key: str = ""
    tls_ca: str = ""
    scan_seconds: float = 2
    max_records: int = 100000
    max_bytes: int = 256000000
    batch_size: int = 40
    critical_burst: int = 4
    direct_shore: bool = False
    dashboard_edge_read: bool = False
    dds_ipe_enabled: bool = False
    tilt: dict = field(default_factory=lambda: {"trigger": 25, "recover": 15, "holdSeconds": 2,
                                                "recoverSeconds": 2, "maxGapSeconds": 4})

    def __post_init__(self):
        if self.role not in ROLES or self.role not in self.nodes:
            raise ValueError("A local endpoint is required for ship, edge or shore")
        if not self.vessel_id or ":" in self.vessel_id:
            raise ValueError("vessel_id must be nonempty and contain no colon")
        if type(self.dashboard_edge_read) is not bool:
            raise ValueError('dashboard_edge_read must be a boolean')
        if type(self.dds_ipe_enabled) is not bool or (self.dds_ipe_enabled and self.role != 'edge'):
            raise ValueError('dds_ipe_enabled is a boolean for the edge role only')
        if not 0.2 <= self.scan_seconds <= 60 or min(self.max_records, self.max_bytes, self.batch_size, self.critical_burst) <= 0:
            raise ValueError("Invalid scan interval or storage/batch limit")
        if self.batch_size <= self.critical_burst:
            raise ValueError("batch_size must leave a slot for ordinary traffic")
        if not 0 <= self.tilt['recover'] < self.tilt['trigger'] <= 180:
            raise ValueError("Tilt recovery threshold must be below trigger")
        if min(self.tilt[k] for k in ('holdSeconds', 'recoverSeconds', 'maxGapSeconds')) <= 0:
            raise ValueError("Tilt timing must be positive")

    @classmethod
    def load(cls, filename):
        path = Path(filename).resolve()
        data = json.loads(path.read_text())
        data['nodes'] = {k: Endpoint(**v) for k, v in data['nodes'].items()}
        for field_name in ('token_file', 'status_token_file', 'tls_cert', 'tls_key', 'tls_ca'):
            if data.get(field_name) and not Path(data[field_name]).is_absolute():
                data[field_name] = str((path.parent / data[field_name]).resolve())
        for endpoint in data['nodes'].values():
            if isinstance(endpoint.ca, str) and not Path(endpoint.ca).is_absolute():
                object.__setattr__(endpoint, 'ca', str(path.parent / endpoint.ca))
        settings = cls(**data)
        from .tls import require_https
        require_https(settings.notify_url)
        for endpoint in settings.nodes.values():
            require_https(endpoint.url)
        if not all((settings.tls_cert, settings.tls_key, settings.tls_ca)):
            raise ValueError('Deployment config requires tls_cert, tls_key and tls_ca')
        return settings
