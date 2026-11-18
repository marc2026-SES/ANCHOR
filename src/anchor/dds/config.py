"""Six configurable DDS policies, with finite process/cache limits."""
from dataclasses import asdict, dataclass
import ipaddress
import json
from pathlib import Path
import re


@dataclass(frozen=True)
class QoS:
    reliability: str = 'BEST_EFFORT'
    durability: str = 'VOLATILE'
    history: str = 'KEEP_LAST'
    depth: int = 20
    deadline_ms: int = 300
    lifespan_ms: int = 1500
    liveliness: str = 'AUTOMATIC'
    lease_ms: int = 2000

    def __post_init__(self):
        choices = {'reliability': ('BEST_EFFORT', 'RELIABLE'),
                   'durability': ('VOLATILE', 'TRANSIENT_LOCAL'),
                   'history': ('KEEP_LAST', 'KEEP_ALL'),
                   'liveliness': ('AUTOMATIC', 'MANUAL_BY_TOPIC')}
        for name, values in choices.items():
            if getattr(self, name) not in values:
                raise ValueError('Unsupported DDS ' + name)
        for name, maximum in [('depth', 256), ('deadline_ms', 60000),
                              ('lifespan_ms', 60000), ('lease_ms', 60000)]:
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError('Invalid DDS ' + name)

    def policies(self, writer=False):
        from cyclonedds.qos import Qos, Policy
        ns = lambda ms: ms * 1000000
        items = [Policy.Reliability.BestEffort if self.reliability == 'BEST_EFFORT'
                 else Policy.Reliability.Reliable(max_blocking_time=ns(20)),
                 Policy.Durability.Volatile if self.durability == 'VOLATILE'
                 else Policy.Durability.TransientLocal,
                 Policy.History.KeepLast(self.depth) if self.history == 'KEEP_LAST'
                 else Policy.History.KeepAll,
                 Policy.Deadline(ns(self.deadline_ms)),
                 Policy.Liveliness.Automatic(ns(self.lease_ms)) if self.liveliness == 'AUTOMATIC'
                 else Policy.Liveliness.ManualByTopic(ns(self.lease_ms)),
                 # A fixed safety cap, not a seventh user-configurable QoS feature.
                 Policy.ResourceLimits(max_samples=256, max_instances=1, max_samples_per_instance=256)]
        if writer:
            items.append(Policy.Lifespan(ns(self.lifespan_ms)))
        return Qos(*items)  # Lifespan is a writer policy; the IPE also rejects stale input.

    def describe(self):
        return asdict(self)


@dataclass(frozen=True)
class Config:
    mode: str
    core_config: str
    interface: str = '127.0.0.1'
    peers: tuple = ('127.0.0.1',)
    domain_id: int = 42
    topic: str = 'AnchorVibrationV1'
    qos: QoS = QoS()
    window_ms: int = 1000
    retention_seconds: int = 60
    mqtt_host: str = 'ship.local'
    mqtt_port: int = 8883

    def __post_init__(self):
        if self.mode not in ('publisher', 'ipe'):
            raise ValueError('DDS mode must be publisher or ipe')
        for address in (self.interface, *self.peers):
            if not isinstance(ipaddress.ip_address(address), ipaddress.IPv4Address):
                raise ValueError('DDS deployment requires explicit IPv4 addresses')
        if not self.peers or len(self.peers) > 8:
            raise ValueError('Configure 1..8 DDS peer addresses')
        if type(self.domain_id) is not int or not 0 <= self.domain_id <= 100:
            raise ValueError('DDS domain_id must be 0..100')
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', self.topic):
            raise ValueError('Invalid DDS topic')
        if type(self.window_ms) is not int or not 200 <= self.window_ms <= 2000:
            raise ValueError('DDS oneM2M window must be 200..2000 ms')
        if type(self.retention_seconds) is not int or not 5 <= self.retention_seconds <= 3600:
            raise ValueError('DDS CSE retention must be 5..3600 seconds')
        if type(self.mqtt_port) is not int or not 1 <= self.mqtt_port <= 65535:
            raise ValueError('Invalid MQTTS port')

    @classmethod
    def load(cls, filename):
        path = Path(filename).resolve()
        value = json.loads(path.read_text())
        value['qos'] = QoS(**value.get('qos', {}))
        value['peers'] = tuple(value.get('peers', ['127.0.0.1']))
        value['core_config'] = str((path.parent / value['core_config']).resolve())
        return cls(**value)

    def xml(self):
        # Explicit unicast UDP avoids silently using Wi-Fi multicast or TCP fallback.
        peers = ''.join(f'<Peer Address="{p}"/>' for p in self.peers)
        return f'''<CycloneDDS><Domain Id="{self.domain_id}">
<General><Interfaces><NetworkInterface address="{self.interface}" multicast="false"/></Interfaces>
<Transport>udp</Transport><AllowMulticast>false</AllowMulticast></General>
<Discovery><ParticipantIndex>auto</ParticipantIndex><MaxAutoParticipantIndex>10</MaxAutoParticipantIndex>
<Peers>{peers}</Peers></Discovery></Domain></CycloneDDS>'''
