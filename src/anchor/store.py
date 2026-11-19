"""CSE-backed append-only journals. No second telemetry database or blind ACK cache."""
import json
import threading
from contextlib import contextmanager

from .client import OneM2MError, canonical, resource
from .events import record_name


class CapacityError(RuntimeError):
    pass


class Journal:
    def __init__(self, client, settings):
        self.client, self.settings = client, settings
        self.lock = threading.RLock()
        self._batch = threading.local()  # per-thread: ticks of different workers do not block each other

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

    @contextmanager
    def batch(self):
        """Read each container's cni/cbs once for a run of writes instead of once per record.
        The snapshot is advanced by this thread's own creates; like the per-record read it
        is a check, not a reservation against other writers."""
        outer = getattr(self._batch, 'counts', None)
        self._batch.counts = {} if outer is None else outer
        try:
            yield self
        finally:
            self._batch.counts = outer

    def _container(self, name):
        counts = getattr(self._batch, 'counts', None)
        if counts is None:
            return resource(self.client.get(self.path(name)))
        if name not in counts:
            counts[name] = dict(resource(self.client.get(self.path(name))))
        return counts[name]

    def put(self, name, identity, value):
        # All writes to a local runtime's journal are serialized. Remote peers also
        # check capacity; count+create is not a distributed reservation protocol.
        with self.lock:
            cnt = self._container(name)
            limited = any(cnt.get(k) is not None for k in ('mni', 'mbs', 'mia'))
            full = (cnt.get('cni', 0) >= self.settings.max_records
                    or cnt.get('cbs', 0) + len(canonical(value).encode()) > self.settings.max_bytes)
            if limited or full:
                # A record that is already stored is still acknowledged while writes are paused.
                old = self.get(name, identity)
                if old is not None:
                    if old != value:
                        raise ValueError(f'Conflicting immutable record: {name}/{identity}')
                    return False
                if limited:
                    raise CapacityError(f'{name} has automatic eviction enabled; run bootstrap with limits disabled')
                raise CapacityError(f'{name} is full; accepted records retained, new writes paused')
            # Create first: the CSE rejects a duplicate name (4105) and put_record then
            # compares the stored body, so no separate existence read is needed.
            created = self.client.put_record(self.path(name), record_name(identity), value)
            if created and getattr(self._batch, 'counts', None) is not None:
                cnt['cni'] = cnt.get('cni', 0) + 1
                cnt['cbs'] = cnt.get('cbs', 0) + len(canonical(value).encode())
            return created
