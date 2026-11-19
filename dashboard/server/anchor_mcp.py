#!/usr/bin/env python3
"""ANCHOR MCP server: read-only oneM2M/monitor data as agent tools.

An AI agent (Claude Desktop, Claude Code, any MCP client) can query the live
ANCHOR system: link state, data continuity, ship AI, coastal fleet AI.
All tools are READ-ONLY by design: this server holds the status token only,
never the core control token, so an agent can observe but not command.

Setup:
    pip install "mcp[cli]" requests

Environment (set in the MCP client config):
    ANCHOR_API         monitor base URL, e.g. https://100.98.242.10:8795
    ANCHOR_TOKEN_FILE  path to the read-only status token file
    ANCHOR_CA          path to the team CA certificate (ca.crt)

Claude Desktop config (claude_desktop_config.json):
    "anchor": {
      "command": "python3",
      "args": ["/path/to/anchor_mcp.py"],
      "env": { "ANCHOR_API": "https://100.98.242.10:8795",
               "ANCHOR_TOKEN_FILE": "/path/to/dashboard.token",
               "ANCHOR_CA": "/path/to/ca.crt" }
    }
"""
import os
import time

import requests
from mcp.server.fastmcp import FastMCP

API = os.environ.get('ANCHOR_API', 'https://127.0.0.1:8795').rstrip('/')
CA = os.environ.get('ANCHOR_CA') or True  # a path verifies against the team CA
_tf = os.environ.get('ANCHOR_TOKEN_FILE')
TOKEN = open(_tf).read().strip() if _tf else ''

mcp = FastMCP('anchor')


def api(path):
    r = requests.get(f'{API}{path}', timeout=20, verify=CA,
                     headers={'Authorization': f'Bearer {TOKEN}'} if TOKEN else {})
    r.raise_for_status()
    return r.json()


def run():
    return api('/api/run?after=0')


def age(ms):
    if ms is None:
        return None
    return round((time.time() * 1000 - ms) / 1000, 1)


@mcp.tool()
def link_status() -> dict:
    """Current state of link A (ship-MEC) and link B (MEC-shore), with how much
    data is held on the ship and waiting at the MEC because of cuts."""
    v = run()
    live = v.get('live') or {}
    links = {k: {'up': l.get('up'), 'observed_s_ago': age(l.get('observedAt')),
                 'source': l.get('source')}
             for k, l in (live.get('links') or {}).items()}
    shore_at = [e['shoreAt'] for e in v.get('events', []) if e.get('shoreAt')]
    return {'run': v.get('runId'), 'mode': v.get('mode'), 'links': links,
            'backlog': live.get('backlog'),
            'last_record_at_shore_s_ago': age(max(shore_at)) if shore_at else None,
            'records_confirmed_at_shore': len(shore_at)}


@mcp.tool()
def ship_health() -> dict:
    """Ship AI (anchorAI): state, subsystem status, latest anomalies and forecasts."""
    v = run()
    ai = v.get('ai') or {}
    return {'state': ai.get('state'), 'error': ai.get('error'),
            'source': ai.get('source'), 'status': ai.get('status'),
            'latest_anomalies': (ai.get('anomaly') or [])[-5:],
            'forecasts': (ai.get('prognosis') or [])[-3:],
            'link_forecasts': (ai.get('linkForecast') or [])[-3:]}


@mcp.tool()
def fleet_status() -> dict:
    """Coastal MEC fleet AI: vessels watched, closest pair, route model state."""
    v = run()
    f = v.get('fleet') or {}
    return {'state': f.get('state'), 'error': f.get('error'), 'status': f.get('status')}


@mcp.tool()
def fleet_conflicts(limit: int = 10) -> list:
    """Latest fleet warnings: collision risks, typhoon/surge hazards (with AI
    rerouting), route deviations. Newest first."""
    v = run()
    f = v.get('fleet') or {}
    out = sorted(f.get('conflicts') or [], key=lambda c: c.get('at', 0), reverse=True)
    return out[:max(1, min(limit, 40))]


@mcp.tool()
def system_health() -> dict:
    """Monitor self-check: journal records stored, rejected records, subscription state."""
    return api('/api/health')


if __name__ == '__main__':
    mcp.run()
