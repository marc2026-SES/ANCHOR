"""Public oneM2M HTTP API only; no ACME imports or internal database access."""
from __future__ import annotations

import json
import threading
import uuid
from urllib.parse import quote

import requests

from .config import Endpoint


class OneM2MError(RuntimeError):
    def __init__(self, rsc, body, target):
        self.rsc, self.body, self.target = rsc, body, target
        super().__init__(f"oneM2M {rsc} at {target}: {body}")


def resource(body):
    return next(iter(body.values()))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class Client:
    def __init__(self, endpoint: Endpoint, origin: str):
        self.endpoint, self.origin = endpoint, origin
        self._local = threading.local()
        self._cin_cache = {}
        self._cache_lock = threading.Lock()

    def as_origin(self, origin):
        return Client(self.endpoint, origin)

    @property
    def root(self):
        return self.endpoint.root

    def request(self, method, target, body=None, ty=None, params=None, headers=None):
        from .tls import require_https
        require_https(self.endpoint.url)
        if not hasattr(self._local, 'session'):
            self._local.session = requests.Session()
            self._local.session.trust_env = False
        path = '/~' + target if target.startswith(self.endpoint.csi + '/') else '/' + target.lstrip('/')
        h = {'X-M2M-Origin': self.origin, 'X-M2M-RI': uuid.uuid4().hex,
             'X-M2M-RVI': self.endpoint.rvi, 'Accept': 'application/json'}
        if body is not None:
            h['Content-Type'] = 'application/json' + (f';ty={ty}' if ty else '')
        h.update(headers or {})
        response = self._local.session.request(method, self.endpoint.url.rstrip('/') + quote(path, safe='/~:'),
                                              json=body, params=params, headers=h,
                                              timeout=self.endpoint.timeout, verify=self.endpoint.ca,
                                              allow_redirects=False)
        if 300 <= response.status_code < 400:
            raise ValueError('CSE redirects are disabled')
        try:
            data = response.json() if response.content else {}
        except ValueError:
            data = {'error': response.text[:300]}
        rsc = int(response.headers.get('X-M2M-RSC', 5000))
        if not 2000 <= rsc < 3000:
            raise OneM2MError(rsc, data, target)
        return data

    def get(self, path, **kwargs):
        return self.request('GET', path, **kwargs)

    def create(self, path, ty, body):
        return self.request('POST', path, body, ty)

    def update(self, path, body):
        return self.request('PUT', path, body)

    def notify(self, path, body):
        return self.request('POST', path, body)

    def ensure(self, parent, ty, body):
        path = parent + '/' + resource(body)['rn']
        try:
            value = self.get(path)
        except OneM2MError as exc:
            if exc.rsc != 4004:
                raise
            try:
                value = self.create(parent, ty, body)
            except OneM2MError as conflict:
                if conflict.rsc not in (4105, 4117):
                    raise
                value = self.get(path)
        if resource(value).get('ty') != ty:
            raise ValueError(f'Wrong resource type: {path}')
        return value

    def children(self, path, **filters):
        refs, seen, offset = [], set(), 1
        while offset <= 1000000:
            page = self.get(path, params={'fu': 1, 'lvl': 1, 'drt': 1,
                                         'lim': 100, 'ofst': offset, **filters})
            fresh = [r for r in page.get('m2m:uril', []) if r not in seen]
            # ACME can repeat the final page. Never rely on a short page alone.
            if not fresh:
                return refs
            refs.extend(fresh)
            seen.update(fresh)
            offset += 100
        raise RuntimeError(f'Discovery did not converge: {path}')

    def cins(self, path):
        expected = resource(self.get(path)).get('cni', 0)
        rows = []
        for ref in self.children(path, drt=2):
            # CIN content is immutable. Re-discover membership every scan so an
            # evicted/deleted resource can never survive as a phantom record.
            with self._cache_lock:
                item = self._cin_cache.get(ref)
            if item is None:
                item = resource(self.get(ref))
            if item.get('ty') == 4:
                with self._cache_lock:
                    self._cin_cache[ref] = item
                rows.append(item)
        if len(rows) < expected:
            raise RuntimeError(f'Incomplete discovery: {path}, expected {expected}, got {len(rows)}')
        return rows

    def put_record(self, parent, name, value):
        payload = canonical(value)
        created = True
        try:
            self.create(parent, 4, {'m2m:cin': {'rn': name, 'cnf': 'application/json:0', 'con': payload}})
        except OneM2MError as exc:
            if exc.rsc != 4105:
                raise
            created = False
        stored = resource(self.get(parent + '/' + name))['con']
        if (json.loads(stored) if isinstance(stored, str) else stored) != value:
            raise ValueError(f'Conflicting immutable record: {parent}/{name}')
        return created
