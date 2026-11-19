"""Public oneM2M HTTP API only; no ACME imports or internal database access."""
from __future__ import annotations

import json
import threading
import time
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
        self._cin_by_ri = {}   # CINs this client created; content is immutable once stored
        self._listings = {}    # container path -> last verified child listing
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

    def children(self, path, start=1, enough=None, **filters):
        # ACME answers an offset past the end of the list with the complete, unpaged list
        # (seconds per thousand records once ACPs are checked). A caller that knows how many
        # records it needs passes `enough` so that final probing request is never sent.
        refs, seen, offset = [], set(), start
        while offset <= 1000000:
            page = self.get(path, params={'fu': 1, 'lvl': 1, 'drt': 1,
                                         'lim': 100, 'ofst': offset, **filters})
            fresh = [r for r in page.get('m2m:uril', []) if r not in seen]
            # ACME can repeat the final page. Never rely on a short page alone.
            if not fresh:
                return refs
            refs.extend(fresh)
            seen.update(fresh)
            if enough is not None and enough(refs):
                return refs
            offset += 100
        raise RuntimeError(f'Discovery did not converge: {path}')

    # A full child listing costs one discovery page per 100 records, and the CSE checks the
    # ACP of every record on a page. Between full listings only the tail of the child list
    # is read (children are listed in creation order), and the result is accepted only if it
    # is consistent with the container's own cni. A deletion or eviction shifts the list and
    # breaks that count, which forces a full listing; a full listing is also repeated at
    # least every FULL_LISTING_SECONDS, so a removed resource cannot survive as a phantom.
    # Unpaged or createdAfter discovery is not used: the CSE then evaluates every child
    # (tens of seconds for a few thousand records) and applies lim before the filter.
    FULL_LISTING_SECONDS = 900
    LISTING_OVERLAP = 8

    def _cin(self, ref):
        with self._cache_lock:
            item = self._cin_cache.get(ref) or self._cin_by_ri.get(ref.rsplit('/', 1)[-1])
        if item is None:
            item = resource(self.get(ref))
        if item.get('ty') == 4:
            with self._cache_lock:
                self._cin_cache[ref] = item
        return item

    def _listed(self, refs):
        kept, rows = [], []
        for ref in refs:
            item = self._cin(ref)
            if item.get('ty') == 4:
                kept.append(ref)
                rows.append(item)
        return kept, rows

    def _count(self, path):
        cnt = resource(self.get(path))
        return cnt.get('cni', 0), cnt.get('st')

    def cins(self, path):
        expected, tag = self._count(path)
        with self._cache_lock:
            known = self._listings.get(path)
        now = time.monotonic()
        refs = rows = settled = None
        full_at = now
        if known and now - known['fullAt'] < self.FULL_LISTING_SECONDS and expected >= len(known['rows']):
            if tag is not None and tag == known['st'] and expected == len(known['rows']):
                return list(known['rows'])
            missing = expected - len(known['rows'])
            if missing == 0:
                # Same count as the verified listing; the periodic full listing covers a
                # delete-and-create that leaves the count unchanged.
                refs, rows, full_at, settled = known['refs'], known['rows'], known['fullAt'], tag
            else:
                seen = set(known['refs'])
                tail = self.children(path, start=max(1, len(known['refs']) - self.LISTING_OVERLAP),
                                     enough=lambda found: sum(r not in seen for r in found) >= missing, drt=2, ty=4)
                added_refs, added_rows = self._listed([r for r in tail if r not in seen])
                total = len(known['rows']) + len(added_rows)
                if total >= expected:
                    # Records keep arriving while the listing runs, so bracket the result with
                    # a second count: fewer live records than listed means one was removed.
                    live, live_tag = (expected, tag) if total == expected else self._count(path)
                    if total <= live:
                        refs, rows = known['refs'] + added_refs, known['rows'] + added_rows
                        full_at = known['fullAt']
                        settled = live_tag if total == live else None
        if rows is None:
            listed = self.children(path, enough=lambda found: len(found) >= expected, drt=2, ty=4) if expected else []
            refs, rows = self._listed(listed)
            settled = tag if len(rows) == expected else None
        if len(rows) < expected:
            raise RuntimeError(f'Incomplete discovery: {path}, expected {expected}, got {len(rows)}')
        with self._cache_lock:
            self._listings[path] = {'st': settled, 'refs': refs, 'rows': rows, 'fullAt': full_at}
        return list(rows)

    def put_record(self, parent, name, value):
        payload = canonical(value)
        created, item = True, None
        try:
            item = resource(self.create(parent, 4, {'m2m:cin': {'rn': name, 'cnf': 'application/json:0', 'con': payload}}))
        except OneM2MError as exc:
            if exc.rsc != 4105:
                raise
            created = False
        # The create response is the CSE's own representation of what it stored. Read the
        # record back only when that response does not carry the content (or on a conflict).
        if not isinstance(item, dict) or 'con' not in item:
            item = resource(self.get(parent + '/' + name))
        stored = item['con']
        if (json.loads(stored) if isinstance(stored, str) else stored) != value:
            raise ValueError(f'Conflicting immutable record: {parent}/{name}')
        if item.get('ty') == 4 and item.get('ri'):
            with self._cache_lock:
                self._cin_by_ri[item['ri']] = item
        return created
