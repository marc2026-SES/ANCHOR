# MEC Sandbox sessions and sandbox lifetime

How to keep the `anchor-edge-mec` scenario running for the whole hackathon. (Written 2026-11-18)

## Summary

- Logging in to the web UI (`https://100.96.155.4`) creates a **sandbox bound to that login session**.
  That sandbox is **deleted as soon as you log out, or when the session is not refreshed for 20 minutes.**
- `sbx3btt489` is now a **sandbox recreated through the platform API, with no session**. Logouts and timeouts do not delete it.
- The web UI does not show this sandbox. Use the map at `http://100.96.155.4:30793`.

## Why sandboxes get deleted (AdvantEDGE source)

| Location | What it does |
|---|---|
| `go-packages/meep-sessions/session-store.go` | `SessionDuration = 1200` (20 min). An open web UI tab refreshes it periodically |
| `go-packages/meep-sessions/session-mgr.go` | Watchdog runs every minute and finds sessions past `timestamp + 20 min` |
| `go-apps/meep-auth-svc/server/auth-svc.go` `sessionTimeoutCb` | Calls `DeleteSandbox` if the timed-out session has a non-empty `Sandbox` |
| `go-apps/meep-auth-svc/server/auth-svc.go` `asLogout` | Deletes the session's sandbox immediately on logout |

- Sessions live in redis (`meep-redis-master-0`, DB 0) as hashes `data:global:sessions:<session ID>` with fields `user`, `sbox`, `timestamp`, etc.
- A sandbox created directly through `platform-ctrl` belongs to no session, so neither the watchdog nor logout deletes it.

## Incident on 2026-11-18

1. `sbx3btt489` was bound to a web login (GitHub account) session.
2. At 07:32:51 UTC the web UI sent a logout; `asLogout: Deleting sandbox: sbx3btt489` ran and every pod disappeared.
3. A session-less sandbox with the same name was created and the scenario reactivated (procedure below).
4. Impact: all CSE data was reset. `ship_link.py` re-copied recent Pi data. The shore `cse-in/anchorAI` had to be recreated by the AI side. Modified ship routes (GIS) went back to the scenario defaults.
5. One session hash with only an empty `sbox` field was left behind (from testing during recovery). Because `sbox` is empty, the watchdog just cleans it up without deleting anything.

## Recovery procedure (if the sandbox is deleted)

Run on the server. No authentication is needed.

```bash
P=$(kubectl -n default get svc meep-platform-ctrl -o jsonpath='{.spec.clusterIP}')

# 1) Create a session-less sandbox (same name)
curl -s -X POST -H 'Content-Type: application/json' -d '{"scenarioName":""}' \
  http://$P/platform-ctrl/v1/sandboxes/sbx3btt489

# 2) Wait for sandbox-ctrl
until kubectl -n sbx3btt489 get pods --no-headers 2>/dev/null | grep meep-sandbox-ctrl | grep -q Running; do sleep 4; done
sleep 20

# 3) Activate the scenario (anchor-core-handover stored in platform-ctrl; anchor-edge-mec is its base)
B=$(kubectl -n sbx3btt489 get svc meep-sandbox-ctrl -o jsonpath='{.spec.clusterIP}')
curl -s -X POST http://$B/sandbox-ctrl/v1/active/anchor-core-handover

# 4) Start ship movement (the web UI used to do this)
G=$(kubectl -n sbx3btt489 get svc meep-gis-engine -o jsonpath='{.spec.clusterIP}')
curl -s -X POST "http://$G/gis/v1/automation/MOVEMENT?run=true"
curl -s -X POST "http://$G/gis/v1/automation/MOBILITY?run=true"
# Match the web UI: keep these two off (they rewrite netchar by distance and fight manual/disaster changes)
curl -s -X POST "http://$G/gis/v1/automation/NETWORK-CHARACTERISTICS-UPDATE?run=false"
curl -s -X POST "http://$G/gis/v1/automation/POAS-IN-RANGE?run=false"
```

Check:

```bash
kubectl -n sbx3btt489 get pods | grep anchor          # 8 Running (ship-cse/app/mirror, edge-cse/app, edge-b-cse/app, anchor-fleet)
curl -s http://100.96.155.4:30793/api/geodata | head -c 200   # map server reads 14 ships
curl -s --cacert ~/anchor-secrets/sandbox-tls/ca.crt https://100.96.155.4:30790/health   # {"ready": true, ...}
```

To restart only the scenario (resets data), send DELETE to `sandbox-ctrl/v1/active` and repeat from step 3.

## Cautions

- **Do not deploy `anchor-edge-mec` from the web UI.** A web login creates a separate sandbox; deploying there collides on the NodePorts (30080-30082, 30790-30792), and that sandbox is deleted again on logout.
- The map server (`~/anchor-map/map_server.py`) is pinned to `sbx3btt489` (override with the `MAP_SANDBOX` env var), so it never picks a web-login sandbox (e.g. `sbx9wx9ovd`, created 11/18).
- Logging in and out of the web UI is fine. It does not affect `sbx3btt489`; only the sandbox created by that session is deleted.
- Deploy scenarios from either the web UI or the API, never both at once (that caused `chart path not found`).
- Recreating the sandbox keeps the MEC API URLs (`https://100.96.155.4/sbx3btt489/...`). CSE and GIS data are reset.
- After a server reboot, check `~/anchor-proxy/start.sh` (in crontab) and `~/anchor-map/start.sh` (not in crontab, start manually).

## Coastal handover (core relay, since 2026-11-19 22:00)

Active scenario: `anchor-core-handover` (core branch relay, `deploy/mec/README.md`).

| Process | Host | Image | Role |
|---|---|---|---|
| `anchor-ship-cse` | UE ship-001 | `anchor-acme` | sandbox ship mirror CSE (HTTPS) |
| `anchor-ship-mirror` | UE ship-001 | `anchor-mirror` | copies the real ship CSE into the mirror (read-only on the Pi) |
| `anchor-ship-app` | UE ship-001 | `anchor-core-relay` | ship relay: SQLite queue, picks coast A/B from MEC 013 |
| `anchor-edge-cse` / `-app` | mep1 | `anchor-acme` / `anchor-core-relay` | coast A `cse-edge` :30080, relay :30790 |
| `anchor-edge-b-cse` / `-app` | mep2 | `anchor-acme` / `anchor-core-relay` | coast B `cse-edge-b` :30084, relay :30794 |

Build and render (from the repo root, certificates once with `deploy/mec/make_certs.sh`):

```bash
R=meep-docker-registry:30001; T=$(git rev-parse --short HEAD)
docker build -f deploy/mec/Dockerfile.acme   -t $R/anchor-acme:$T . && docker push $R/anchor-acme:$T
docker build -f deploy/mec/Dockerfile.mirror -t $R/anchor-mirror:$T . && docker push $R/anchor-mirror:$T
docker build -f deploy/mec/Dockerfile.relay  -t $R/anchor-core-relay:$T . && docker push $R/anchor-core-relay:$T
IMAGE_TAG=$T python3 deploy/mec/make_base_scenario.py anchor-korea-strait.json /private/anchor-edge-mec.json
python3 deploy/mec/make_scenario.py /private/anchor-edge-mec.json /private/anchor-core-handover.json \
  --image $R/anchor-core-relay:$T --mirror-command /usr/local/bin/anchor-mirror
# POST it to platform-ctrl /scenarios/anchor-core-handover, then activate (steps above)
```

- Store the scenario JSON outside the repo: it carries the sandbox TLS key and tokens.
- POST the new scenario and check for 200 **before** deactivating the running one (a 400 leaves the sandbox empty).
- Everything in the sandbox is HTTPS with the sandbox CA (`~/anchor-secrets/sandbox-tls/ca.crt`). The relay status is `GET https://100.96.155.4:<port>/status` with the API token.
- Measured 2026-11-19: Busan -> Kyushu switch, first store at coast B 0.23 s after the relay saw the zone change.
- Known issue: the real ship relay also writes the same events straight to the real shore. The sandbox copy carries the mirror's `source.ri/ct`, so the shore answers "Stored CIN content differs" and the coast forwarder stops at that record. Fix by sending the real ship through the sandbox coast (one path), or give the sandbox stream its own vessel ID.

## Shore = the real Pi (since 2026-11-19)

The sandbox no longer runs a shore. The edge core (`anchor-edge-app` on mep1) forwards to the **real shore Pi** at `https://100.98.242.10:8443/cse-in`.

- TLS is verified with the team CA `pi-ca.crt` (`~/anchor-secrets/secrets/`). `make_scenario.py` embeds it as `SHORE_CA_B64`.
- `SHORE_TIMEOUT=30`: the Pi answers deep discovery pages slowly (up to ~15 s).
- The real shore's ACP already lets `CAnchorEdge` write its journals; nothing was changed on the Pi.
- The edge CSE no longer registers anywhere (`REGISTRAR_URL` is a dummy); ANCHOR talks to each CSE directly and does not need a CSR.
- Next step (needs the Pi team): an edge server certificate signed by the Pi CA (SAN `IP:100.96.155.4`) and the real ship core pointed at the sandbox edge. Then the ship mirror can be removed.
- 2026-11-19 21:40: the real shore no longer runs an ANCHOR shore core (removed on the `core` branch). Its `anchor/events` now evicts at `mni=2000` and `receipts` stays empty, so the coasts get no final receipts (`shoreConfirmed` stays 0) and journal reads race with eviction (4004). Final-delivery checks need a shore core again (real Pi or a sandbox shore).

## Endpoints

| Purpose | Address |
|---|---|
| Shared map / position JSON | `http://100.96.155.4:30793`, `/api/geodata` |
| Disasters (typhoon, tsunami) | `GET/POST /api/hazards`, `DELETE /api/hazards/{id or all}` on the map server |
| Change a ship's route | `POST /api/ships/{name}/route` on the map server (rejects paths over land) |
| Land/sea test | `GET /api/land?lon=..&lat=..` on the map server |
| MEC APIs | `https://100.96.155.4/sbx3btt489/{mep1,mep2}/location/v3/...`, `.../gis/v1/...` |
| CSEs in the sandbox, HTTPS (ship mirror / coast A / coast B) | `:30082/cse-ship`, `:30080/cse-edge`, `:30084/cse-edge-b` |
| Core APIs in the sandbox, HTTPS (ship / coast A / coast B) | `:30792`, `:30790`, `:30794` |
| Real ship Pi (outside the sandbox) | ANCHOR CSE `https://100.114.83.111:8443/cse-ship`, core `https://100.114.83.111:8790`, legacy hardware CSE `http://100.114.83.111:8080/cse-ship` |
| Real shore Pi (outside the sandbox) | ANCHOR CSE `https://100.98.242.10:8443/cse-in`, core `https://100.98.242.10:8790`, legacy hardware CSE `http://100.98.242.10:8080/cse-in` |
