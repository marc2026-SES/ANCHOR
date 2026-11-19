# ANCHOR core

This Python package handles oneM2M delivery between the ship, the coastal edge, and shore. The [ship relay](../../hardware/ship-gateway-pi5/sync/anchor_relay.py) is the entry point for sensor subscriptions and the ship's SQLite queue. In the MEC deployment, the [container launcher](../../deploy/mec/start_relay.py) starts the relay in ship or edge mode.

## Follow a sensor event

1. **Ship.** A sensor notification reaches the relay. It assigns the reading a stable event ID and queues it in SQLite. In MEC mode, the relay selects the Busan or Kyushu edge from the ship's location. The direct configuration sends to the shore CSE. [Handover logic](handover.py) · [Direct relay configuration](../../deploy/ship/relay.json)
2. **Delivery to the next hop.** The relay creates a content instance under `anchor/events`. If the destination reports a duplicate name (`4105`), the relay reads that one instance and compares its body. It removes the queued record after a successful create or a verified match. [Event sender](handover.py) · [oneM2M client](client.py)
3. **Edge to shore.** Each edge forwards the records it accepted, including after the ship moves away. It preserves the resource name and body and saves its progress in SQLite so it can resume after a restart. The shore CSE's event journal is where to verify final arrival. [Edge forwarder](edge_forward.py)

## Other modules

| Module | Purpose |
| --- | --- |
| [Provisioning](provision.py) and [resources](resources.py) | Create role-specific oneM2M resources and apply access control policies. |
| [Sensor models](sdt.py) | Publish ship position, engine vibration RMS, and cargo temperature as Smart Device Template resources. |
| [Group control demo](group_demo.py) | Send one reporting profile to the bridge and engine devices and check their individual results. |
| [Configuration](config.py), [TLS](tls.py), and [ACME adapter](acme_tls.py) | Define CSE endpoints and configure authenticated transport. |

## Optional DDS vibration stream

ESP32 nodes send measurements to the ship gateway over MQTT with TLS (MQTTS, port 8883). The MEC relays forward oneM2M event records, including vibration summaries, over HTTPS.

The optional [DDS package](dds/) handles raw accelerometer samples. When explicitly started, a ship-side publisher subscribes to the raw MQTTS feed and republishes it over DDS. A coastal interworking proxy (IPE) reads that stream and writes waveform windows to its local oneM2M CSE. The MEC handover relay runs independently of both DDS processes.

The supplied [edge host configuration](../../deploy/edge/host.json) enables DDS resource provisioning. Consequently, `acp-provision` imports `dds.resources` on that host. Starting the waveform service is a separate step through `anchor-dds` or the [Compose `dds` profile](../../deploy/edge/compose.yaml).

For installation and startup, see the [project README](../../README.md) and the [MEC deployment guide](../../deploy/mec/README.md).
