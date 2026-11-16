"""CSE-backed append-only journals. No second telemetry database or blind ACK cache."""
import json
import threading

from .client import OneM2MError, canonical, resource
from .events import record_name


class CapacityError(RuntimeError):
    pass


class Journal:
    def __init__(self, client, settings):
        self.client, self.settings = client, settings
        self.lock = threading.RLock()

    def path(self, name):
        return f'{self.client.root}/anchor/{name}'

    def get(self, name, identity):
        try:
            item = resource(self.client.get(self.path(name) + '/' + record_name(identity)))
        except OneM2MError as exc:
            if exc.rsc == 4004:
                return None
            raise
        return json.loads(item['con']) if isinstance(item['con'], str) else item['con']

    def rows(self, name):
        return self.client.cins(self.path(name))

    def values(self, name):
        for cin in self.rows(name):
            yield json.loads(cin['con']) if isinstance(cin['con'], str) else cin['con']

    def put(self, name, identity, value):
        # All writes to a local runtime's journal are serialized. Remote peers also
        # check capacity; count+create is not a distributed reservation protocol.
        with self.lock:
            old = self.get(name, identity)
            if old is not None:
                if old != value:
                    raise ValueError(f'Conflicting immutable record: {name}/{identity}')
                return False
            cnt = resource(self.client.get(self.path(name)))
            if any(cnt.get(k) is not None for k in ('mni', 'mbs', 'mia')):
                raise CapacityError(f'{name} has automatic eviction enabled; run bootstrap with limits disabled')
            if cnt.get('cni', 0) >= self.settings.max_records or cnt.get('cbs', 0) + len(canonical(value).encode()) > self.settings.max_bytes:
                raise CapacityError(f'{name} is full; accepted records retained, new writes paused')
            return self.client.put_record(self.path(name), record_name(identity), value)
