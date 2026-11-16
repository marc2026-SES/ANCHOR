"""In-memory API test double. Does not claim ACME wire-format/ACP conformance."""
import copy
from datetime import datetime, timezone

from anchor.client import OneM2MError, canonical, resource


class MemoryCSE:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.rows = {endpoint.root: {'ty': 5, 'rn': endpoint.root, 'ri': endpoint.root}}
        self.sequence = 0
        self.online = True
        self.notifications = []

    def client(self, origin='CAdmin'):
        return MemoryClient(self, origin)


class MemoryClient:
    def __init__(self, server, origin):
        self.server, self.origin = server, origin
        self.endpoint = server.endpoint
        self.root = self.endpoint.root

    def as_origin(self, origin):
        return MemoryClient(self.server, origin)

    def check(self):
        if not self.server.online:
            raise ConnectionError('test link unavailable')

    def resolve(self, path):
        if path in self.server.rows:
            return path
        for p, r in self.server.rows.items():
            if r['ri'] == path:
                return p
        return path

    def get(self, path, **kwargs):
        self.check(); path = self.resolve(path)
        if path not in self.server.rows:
            raise OneM2MError(4004, {}, path)
        item = copy.deepcopy(self.server.rows[path])
        if item['ty'] == 3:
            rows = self.cins(path)
            item.update(cni=len(rows), cbs=sum(len(canonical(r['con']).encode()) for r in rows))
        return {'resource': item}

    def create(self, parent, ty, body):
        self.check()
        if parent.endswith('/fopt'):
            group = resource(self.get(parent[:-5]))
            replies = []
            for member in group['mid']:
                try:
                    self.create(self.resolve(member), ty, body)
                    replies.append({'rsc': 2001})
                except OneM2MError as exc:
                    replies.append({'rsc': exc.rsc})
            return {'m2m:agr': {'m2m:rsp': replies}}
        parent = self.resolve(parent)
        if parent not in self.server.rows:
            raise OneM2MError(4004, {}, parent)
        item = copy.deepcopy(resource(body))
        self.server.sequence += 1
        rn = item.get('rn', 'auto-' + str(self.server.sequence))
        path = parent + '/' + rn
        if path in self.server.rows:
            raise OneM2MError(4105, {}, path)
        item.update(rn=rn, ty=ty, ri='ri-' + str(self.server.sequence),
                    ct=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S,%f'), _owner=self.origin)
        if ty == 2:
            item['aei'] = self.origin
        self.server.rows[path] = item
        return self.get(path)

    def ensure(self, parent, ty, body):
        path = self.resolve(parent) + '/' + resource(body)['rn']
        try:
            return self.get(path)
        except OneM2MError as exc:
            if exc.rsc != 4004:
                raise
            return self.create(parent, ty, body)

    def update(self, path, body):
        self.check(); path = self.resolve(path)
        attrs = resource(body)
        row = self.server.rows[path]
        if row['ty'] == 9 and 'mt' in attrs:
            raise OneM2MError(4000, 'memberType is not updatable', path)
        if 'acpi' in attrs:
            assert len(attrs) == 1, 'oneM2M acpi must be updated alone'
            if not row.get('acpi') and self.origin != row['_owner']:
                raise OneM2MError(4103, {}, path)
        row.update(copy.deepcopy(attrs))
        return self.get(path)

    def children(self, path, **filters):
        self.check(); path = self.resolve(path)
        return [p for p in self.server.rows if p.startswith(path + '/') and '/' not in p[len(path)+1:]]

    def cins(self, path):
        return [copy.deepcopy(self.server.rows[p]) for p in self.children(path) if self.server.rows[p]['ty'] == 4]

    def put_record(self, parent, name, value):
        try:
            self.create(parent, 4, {'m2m:cin': {'rn': name, 'con': canonical(value)}})
            return True
        except OneM2MError as exc:
            if exc.rsc != 4105:
                raise
            import json
            if json.loads(resource(self.get(parent + '/' + name))['con']) != value:
                raise ValueError('Conflicting immutable record')
            return False

    def notify(self, path, body):
        self.check()
        self.server.notifications.append((path, body))
        return {}
