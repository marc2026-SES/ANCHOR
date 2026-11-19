"""Explicit endpoints: the coastal CSE must never be reached through shore."""
from __future__ import annotations

import json
from dataclasses import dataclass
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
    tls_cert: str = ""
    tls_key: str = ""
    tls_ca: str = ""
    dds_ipe_enabled: bool = False
    dashboard_edge_read: bool = False

    def __post_init__(self):
        if self.role not in ROLES or self.role not in self.nodes:
            raise ValueError("A local endpoint is required for ship, edge or shore")
        if not self.vessel_id or ":" in self.vessel_id:
            raise ValueError("vessel_id must be nonempty and contain no colon")
        if type(self.dashboard_edge_read) is not bool:
            raise ValueError('dashboard_edge_read must be a boolean')
        if type(self.dds_ipe_enabled) is not bool or (self.dds_ipe_enabled and self.role != 'edge'):
            raise ValueError('dds_ipe_enabled is a boolean for the edge role only')

    @classmethod
    def load(cls, filename):
        path = Path(filename).resolve()
        data = json.loads(path.read_text())
        data['nodes'] = {k: Endpoint(**v) for k, v in data['nodes'].items()}
        for field_name in ('tls_cert', 'tls_key', 'tls_ca'):
            if data.get(field_name) and not Path(data[field_name]).is_absolute():
                data[field_name] = str((path.parent / data[field_name]).resolve())
        for endpoint in data['nodes'].values():
            if isinstance(endpoint.ca, str) and not Path(endpoint.ca).is_absolute():
                object.__setattr__(endpoint, 'ca', str(path.parent / endpoint.ca))
        settings = cls(**data)
        from .tls import require_https
        for endpoint in settings.nodes.values():
            require_https(endpoint.url)
        if not all((settings.tls_cert, settings.tls_key, settings.tls_ca)):
            raise ValueError('Deployment config requires tls_cert, tls_key and tls_ca')
        return settings
