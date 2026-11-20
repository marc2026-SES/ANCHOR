# MEC map server (`:30793`)

The shared live map of the MEC Sandbox and the small HTTP API the rest of ANCHOR builds on: fleet-ai (`ai/tools/fleet_scan.py`), federated learning (`ai/fl_run.py`) and the MEC dashboard (`/map/`, `/core/*/status`).

One Python file, standard library only, plus one Leaflet page. It reads the sandbox GIS engine and sandbox controller through `kubectl`, so it runs on the MEC server.

```bash
deploy/mec/map/start.sh          # starts once (no-op if running), logs to map.log
# restart: pkill -f "^python3 .*map_server.py"; deploy/mec/map/start.sh
```

## What it shows

- 14 ships with their routes, 9 base stations with coverage, all from the sandbox GIS engine every 2 s
- Base stations coloured by disaster impact (degraded / severe / flooded / down)
- Typhoon and tsunami areas, with the typhoon's forecast track
- fleet-ai collision warnings: a red line between the pair (`cse-edge/fleetAI/conflicts/la`)
- Route changes: a ship that fleet-ai is steering (`by` = `fleet-ai:avoid` / `fleet-ai:return`) gets a bold orange route, its previous route as a dashed line, and a "⟲ by" marker for 15 min; every change is listed for 30 min
- English place names (Esri Ocean tiles). `?embed=1` hides the side panel; `?theme=light|dark` forces a theme

## API

| Endpoint | Method | What it does |
|---|---|---|
| `/api/geodata` | GET | Ships (`routeBy` included), base stations (`impact`), hazards, conflicts, `routeChanges` |
| `/api/hazards` | GET, POST | List or start a disaster (`{"type":"typhoon","center":[lon,lat],...}`) |
| `/api/hazards/{id or all}` | DELETE | Stop a disaster and restore the base stations |
| `/api/ships/{name}/route` | POST | New path `{"path":[[lon,lat],...],"by":"...","reason":"..."}`. Current position is prepended. Waypoints on land or paths crossing land are rejected unless `"allowLand": true` |
| `/api/route-changes?minutes=60` | GET | Route change history: ship, time, `by`, `reason`, old and new path |
| `/api/land?lon=&lat=` | GET | `{"land": true|false}` (Natural Earth 10m, clipped to the strait) |
| `/api/conflicts` | GET | Latest fleet-ai collision warnings |
| `/core/{ship,edge,edge-b,shore}/status` | GET | Read-only relay status (no token needed; the server holds the tokens) |
| `/livemap.js` | GET | Map-only widget for dashboards (`AnchorLiveMap.mount(el, {api})`) |

All responses allow any origin (CORS `*`).

## Disasters

Every 3 s the engine works out each base station's level and pushes network characteristics to the sandbox controller (`NETWORK-CHARACTERISTICS-UPDATE`), only when a level changes. When a disaster passes, expires or is deleted, the station returns to the values stored in the scenario (not the active model, which already contains earlier degradations).

| Level | Where | Added latency | Packet loss | Throughput |
|---|---|---|---|---|
| Degraded | typhoon outer ring | +150 ms | 10 % | 30 % |
| Severe | typhoon core (inner 40 %) | +400 ms | 40 % | 10 % |
| Flooded | inside the tsunami wave | +200 ms | 25 % | 20 % |
| Down | inside the quake radius | +1000 ms | 100 % | 1 % |

## Configuration (environment)

| Variable | Default | Meaning |
|---|---|---|
| `MAP_SANDBOX` | `sbx3btt489` | Sandbox to follow (never picks a web-login sandbox) |
| `CORE_HOST` | `172.19.30.16` | LAN IP for NodePorts (Calico eBPF does not serve them on 127.0.0.1) |
| `ANCHOR_TOKENS` | `deploy/mec/tokens.json` | Relay API tokens (gitignored) |
| `SANDBOX_CA` | `~/anchor-secrets/sandbox-tls/ca.crt` | Sandbox CA (from `deploy/mec/make_certs.sh`) |
| `REAL_SHORE` | `https://100.98.242.10:8443/cse-in` | Shown in `/core/shore/status` |
| `CONFLICTS_URL`, `CONFLICT_MAX_AGE` | edge `fleetAI/conflicts/la`, 120 s | Collision warning source and how long a warning stays |

Runtime files (gitignored): `hazards.json` (active disasters), `route_changes.json` (last 300 changes), `map.log`.
