# anchor-fleet

The fleet-wide MEC application of the ANCHOR scenario. One instance runs on `mep1`, and it handles every UE in the scenario (vessels, buoys, cameras).

- Inventory: reads the active scenario from sandbox-ctrl and lists its UEs and the MEC host of each zone.
- Link state: polls the MEC013 Location API every 2 s. It maps each UE to LTE (a coastal zone with a MEC host), VSAT (`VSAT_ZONES`) or NONE (not attached).
- Twins: keeps one oneM2M twin per UE, an AE with a `device` flexContainer (`mec:iotDev`) and the containers `telemetry` and `link`.
- Store-and-forward: buffers telemetry while a UE has no link and replays it in order when the UE is back.
- Faults: injects demo faults through sandbox events (`POST /faults/...`).

## Twin handover between MEC hosts

With `TWIN_HANDOVER=true` (the default), each twin lives on the platform MN-CSE of the MEC host that serves the UE's zone:

| Zone | MEC host | CSE |
|---|---|---|
| `kr-busan` | `mep1` | `http://mep1-meep-acme-mn-cse/acme-cse-mn-mep1-<sandbox>` |
| `jp-kyushu` | `mep2` | `http://mep2-meep-acme-mn-cse/acme-cse-mn-mep2-<sandbox>` |

The app builds the zone → MEC host map from the active scenario (the `EDGE` physical location of each zone).

When a UE crosses into another MEC zone, the app creates the twin on the new CSE first and then deletes it from the old one. It writes the link change and any buffered samples to the new CSE.

Zones without a MEC host (VSAT, or no link) keep the twin where it is, so a ship that drops off the coast does not bounce between CSEs.

Set `TWIN_HANDOVER=false` to keep every twin on the home CSE (`CSE_URL`/`CSE_NAME`, by default the one of `MEP_NAME`).

Verified in `sbx3btt489` on 2026-11-19:
- `ferry-hakata` attached to `fukuoka-lte-4` (`jp-kyushu`), and its twin moved from the mep1 CSE to the mep2 CSE 1 s later.
- The mep1 CSE then returned 404 for it.
- The mep2 CSE held the new link state and the telemetry, including the replayed sample.

## Build and run

```sh
./dockerize.sh                       # builds the Go binary and pushes meep-docker-registry:30001/anchor-fleet
kubectl -n <sandbox> rollout restart deploy/anchor-fleet   # the scenario pulls the image with policy Always
go test -race ./...                  # unit tests (zone map, twin follows the zone)
```

Settings are environment variables; see `main.go`. In the scenario, only `MEP_NAME` and `VSAT_ZONES` are set. The MEEP_* values come from the sandbox.

HTTP API (`HTTP_PORT`, default 8080):
- `GET /health`
- `GET /devices` (each device includes `mep`, the host whose CSE holds its twin)
- `GET /faults/demo`
- `POST /faults/run`
- `POST /faults/{action}/{target}`
