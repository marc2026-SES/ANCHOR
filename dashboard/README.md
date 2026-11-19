# ANCHOR Dashboards

Two dashboards, one codebase, one theme — the shore-side window into ANCHOR's
ship-to-shore data continuity service (oneM2M + MEC).

| View | URL | What it shows |
| --- | --- | --- |
| **IN dashboard** | `https://<host>:8795/?live` | One real vessel (ship-001): data continuity across link cuts — what arrived, what is held, what came back |
| **MEC dashboard** | `https://<host>:8795/?mec` | The whole coastal MEC: live fleet map, fleet AI warnings, federated-learning progress. Clicking the real vessel opens its IN dashboard |

Without `?live` / `?mec` the page runs on bundled demo data (no backend needed).
`?mec` implies live mode; add `&demo` to force demo data.

## Quick start

```bash
npm install
npm run dev        # http://localhost:5180        demo data
                   # http://localhost:5180/?live  live data (Shore Monitor below)
npm run build      # -> dist/ (fully offline: no CDN, Pretendard bundled)
```

## Shore Monitor AE (`server/shore_monitor.py`)

A single Python process (3.9+, stdlib only) backs both views. It registers as the
**read-only** AE `CAnchorDashboard` (ACP: Retrieve + Discovery, plus
subscription-create on the shore events container) and never writes a record.

What it reads:

| What | From | Why |
| --- | --- | --- |
| events, commands (`shoreAt`) | shore CSE `anchor/events`, `anchor/commands` | data that reached the shore is the ground truth |
| coast custody times (`coastAt`) | edge (MEC) CSE, optional | splits delay into hop A and hop B |
| link A/B state, held/waiting counts | ship & edge core `/status`, optional | invisible from the shore CSE alone |
| ship AI / fleet AI results | `anchorAI` (shore or ship), `fleetAI` (edge) | AI tab and MEC fleet card |

Subscriptions (`nct=1`) are treated as a "new data" doorbell only — records are
always re-read from the CSE; a 2 s poll runs as fallback.

It also relays, so the browser needs **one origin, one certificate, zero CORS**:

| Route | Upstream | Used by |
| --- | --- | --- |
| `/cam.mjpg` | ship Pi 5 camera (MJPEG) | Deck camera card |
| `/live.sse` | ship 20 Hz attitude (SSE) | 3D hull, switches 1 Hz ↔ 20 Hz |
| `/map/` | MEC map server | MEC fleet map (framable) |
| `/api/fl` | fl-cse FL coordinator tree | MEC federated-learning panel |

### Configuration (`server/shore-monitor.json`)

Start from `shore-monitor.example.json`. Key sections:

```jsonc
{
  "shore":  { "url": "https://<shore-cse>:8443", "timeout": 30 },
  "edge":   { "url": "http://<edge-cse>:30080", "timeout": 10 },
  "ship":   { "url": "https://<ship-cse>:8443" },          // optional
  "cores":  { "ship": {...}, "edge": {...} },               // /status, read-only token files
  "camera": { "url": "...", "live_url": "..." },            // ship MJPEG + 20 Hz SSE
  "map":    { "url": "http://<map-server>:30793/" },
  "fl":     { "url": "http://localhost:18080/cse-fl" },     // FL panel source
  "ui":     { "in_url": "https://<in-host>:8795/?live" },   // MEC view: where "open IN dashboard" points
  "tls":    { "ca": "...", "cert": "...", "key": "..." },   // team CA; https serving cert
  "api":    { "bind": "0.0.0.0", "port": 8795, "open": true }
}
```

Run:

```bash
cd server
python3 shore_monitor.py --config shore-monitor.json check   # what can it reach and read?
python3 shore_monitor.py --config shore-monitor.json serve   # subscribe, store, serve /api + dashboard
```

## IN dashboard (the continuity story)

- Link badges (LINK A ship↔MEC, LINK B MEC↔shore) from core `/status`
- Phase banner + 10-minute arrival strip: outages appear hatched red the moment
  they happen; once the backlog is replayed the hole turns solid purple
- Continuity timeline and delivery-mix donut (live / delivered-after-waiting / lost),
  colors shared with the strip
- Sensors tab: 3D hull driven by shore records at 1 Hz, switching to the vessel's
  direct 20 Hz stream while link A is up; deck camera (not stored — its gaps stay gaps)
- AI tab: anchorAI status, anomalies (latest 5 + view all), forecasts; critical
  alerts as dismissable toasts
- Playback: scrub any past window and replay it at 1/10/30/60×, even during a live run

## MEC dashboard (the coastal story)

- Fleet map embedded through `/map/`; clicking **ship-001** (label or list) opens
  the IN dashboard (`ui.in_url`)
- Fleet AI card: `fleetStatus` (vessels watched, closest pair CPA/TCPA, route-model
  version) and three warning kinds — collision risk, hazard (typhoon with ETA and
  escape course, green "rerouted by AI" badge), route deviation
- Federated-learning panel: watches the FL coordinator's oneM2M resources via
  `/api/fl` — round dots fill in live, global model version/trees, per-vessel
  local window counts ("stay local")

## AI agent over MCP (`server/anchor_mcp.py`)

Five **read-only** MCP tools (`link_status`, `ship_health`, `fleet_status`,
`fleet_conflicts`, `system_health`) wrap the monitor API, so an AI agent
(e.g. Claude Desktop) can inspect the live system — and only inspect it: the
server holds the read-only status token, never a core control token.

```bash
pip3 install "mcp[cli]<2" requests
# MCP client env: ANCHOR_API=https://<host>:8795  ANCHOR_CA=<team ca.crt>
```

## Federated learning (`../ai/fl_run.py`, `../ai/fed_iforest.py`)

The FL coordinator and participants run against a dedicated `fl-cse` (ACME with
the custom flexContainer types in `../ai/acme/anchor-fl.fcp`: flTask,
flParticipant, flRound, flUpdate, flModel). Only Isolation-Forest trees travel as
content instances; training data never leaves a vessel. Participant selection is
disaster-aware: a vessel whose base station is inside an active hazard
(map `/api/geodata` POA impact) is excluded from the round, and restored when the
quorum requires it. See `../ai/FL_RUN.md` for the full runbook.

## Security notes

- The dashboard AE is read-only by ACP; it cannot create or change records
- Status tokens live in files, are never put in URLs or browser storage, and are
  the only tokens the dashboard side ever holds
- Every CSE/core connection is HTTPS against the team's private CA
  (`scripts/provision-tls.py`); `server/secrets/`, `server/data/` and the live
  config are excluded from git

## Deploy (hackathon layout)

```bash
npm run build
rsync -av dist/ <user>@<in-host>:~/anchor-dashboard/dist/
rsync -av dist/ <user>@<mec-host>:~/anchor-dashboard/dist/
rsync -av server/shore_monitor.py <user>@<each-host>:~/anchor-dashboard/server/
# then on each host: pkill -f shore_monitor.py && nohup python3 shore_monitor.py --config shore-monitor.json serve &
```

One monitor instance per host; the IN host serves `/?live`, the MEC host serves
`/?mec`, and `ui.in_url` on the MEC host links the two.
