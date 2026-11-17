#!/usr/bin/env python3
"""ANCHOR Shore Monitor AE.

Reads the shore journal of the ANCHOR core (cse-in/anchor/events and anchor/commands) as
CAnchorDashboard, keeps one row per event with the CSE creation time of each copy, and serves
it to the dashboard at /api/run. Optional: the coastal journal (coastAt, hop A / hop B split)
and the control API of each core (link A / B state, data still held on the ship).

Only the public oneM2M HTTP API is used, with the same conventions as src/anchor/client.py.
HTTPS is verified against the team's own CA (tls.ca); verification cannot be switched off.
Python 3.9+ standard library only, so it runs on the shore Pi 4 or a laptop without installs.

    python3 shore_monitor.py --config shore-monitor.json check   # what can it reach and read?
    python3 shore_monitor.py --config shore-monitor.json serve   # subscribe, store, serve /api

The CSE notification is only a wake-up hint. Records are always read back from the CSE
(as the core does), so a forged notification cannot put data on the dashboard.
"""
from __future__ import annotations

import argparse
import bisect
import collections
import copy
import hmac
import ipaddress
import json
import mimetypes
import os
import secrets
import logging
import sqlite3
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

log = logging.getLogger('shore-monitor')

DEFAULTS = {
    'origin': 'CAnchorDashboard',
    'ae_name': 'dashboard',
    # Shore CSE (Pi 4). Required.
    'shore': {'url': '', 'root': 'cse-in', 'csi': '/id-in', 'rvi': '3', 'timeout': 3},
    # Coastal CSE. Optional: needs CAnchorDashboard R Di on cse-edge/anchor/events and anchor/commands.
    'edge': {'url': '', 'root': 'cse-edge', 'csi': '/id-edge', 'rvi': '3', 'timeout': 3},
    # Ship CSE. Not read by the monitor; only deny_tests.py uses it (T1–T4).
    'ship': {'url': '', 'root': 'cse-ship', 'csi': '/id-ship', 'rvi': '3', 'timeout': 3},
    # Where the shore CSE sends notifications. url empty = no subscription, scan only.
    'notify': {'bind': '0.0.0.0', 'port': 8796, 'url': '', 'sub_name': 'shore-monitor'},
    # Dashboard API. Keep it on 127.0.0.1 unless a token_file is set (then https with tls.cert).
    # It also serves the built dashboard (npm run build → ../dist) when that folder exists.
    'api': {'bind': '127.0.0.1', 'port': 8795, 'token_file': ''},
    'dashboard_dir': '',
    # Control API of each core (anchor serve, port 8790), for link state and backlog. Optional.
    # Core control APIs read for link state and backlog only. Not the shore core: its token can POST /commands.
    'cores': {role: {'url': '', 'token_file': '', 'timeout': 3} for role in ('ship', 'edge')},
    'scan_seconds': 2,
    'core_seconds': 2,
    # A link counts as down when arrivals stop for longer than this while data was waiting (see infer_outages).
    'infer_gap_seconds': 10,
    'full_scan_seconds': 60,
    'db': 'shore-monitor.db',
    'run': {'id': 'live', 'mode': 'anchor', 'history_minutes': 60},
    # The core copies bridge/position into anchor/events, so lat/lon reach the shore journal even though
    # bridge/position itself is closed to CAnchorDashboard. Drop them before storing unless set to false.
    'redact_position': True,
    # TLS. ca: the team's CA certificate (ca.crt from provision-tls.py), required for any https address.
    # cert/key: a server certificate for this machine, needed only for an https notify.url (on the shore
    # Pi: its own tls/server.crt and server.key) and for an API that listens beyond loopback.
    'tls': {'ca': '', 'cert': '', 'key': ''},
}

POSITION_FIELDS = ('lat', 'lon')

KINDS = ('measurement', 'incident', 'commandResult', 'incidentAck')
SITES = ('coast', 'shore')


# ── config ────────────────────────────────────────────────────────────────────

def merge(base, extra):
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        out[key] = merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def load_config(filename):
    path = Path(filename).resolve()
    cfg = merge(DEFAULTS, json.loads(path.read_text(encoding='utf-8')))
    here = path.parent

    def local(p):
        return str(p if not p or Path(p).is_absolute() else here / p)

    cfg['db'] = local(cfg['db'])
    cfg['api']['token_file'] = local(cfg['api']['token_file'])
    cfg['dashboard_dir'] = local(cfg['dashboard_dir']) or str(Path(__file__).resolve().parent.parent / 'dist')
    if set(cfg['cores']) - {'ship', 'edge'}:
        raise SystemExit('cores: only ship and edge are read. Leave the shore core out: its API token can issue '
                         'commands to the ship, and the monitor never needs it.')
    for core in cfg['cores'].values():
        core['token_file'] = local(core.get('token_file', ''))
    for site in ('shore', 'edge', 'ship'):
        node = cfg[site]
        if node['url'] and urlparse(node['url']).scheme not in ('http', 'https'):
            raise SystemExit(f'{site}.url must start with http:// or https://')
        if not node['root'] or '/' in node['root'] or not node['csi'].startswith('/'):
            raise SystemExit(f'{site}: invalid root or csi')
    if cfg['run']['mode'] not in ('anchor', 'baseline'):
        raise SystemExit('run.mode must be anchor or baseline')
    tls = cfg['tls']
    for key in ('ca', 'cert', 'key'):
        tls[key] = local(tls[key])
        if tls[key] and not Path(tls[key]).is_file():
            raise SystemExit(f'tls.{key}: file not found: {tls[key]}')
    urls = [cfg[s]['url'] for s in ('shore', 'edge', 'ship')] + [c['url'] for c in cfg['cores'].values()]
    if any(u.startswith('https://') for u in urls) and not tls['ca']:
        raise SystemExit('https addresses need tls.ca: the CA certificate (ca.crt) the team issued. '
                         'Certificate verification is never switched off.')
    for node in [cfg[s] for s in ('shore', 'edge', 'ship')] + list(cfg['cores'].values()):
        node['ca'] = tls['ca']
    notify = cfg['notify']['url']
    if notify:
        scheme = urlparse(notify).scheme
        if cfg['shore']['url'].startswith('https://') and scheme != 'https':
            raise SystemExit('notify.url must be https: the shore CSE sends notifications over HTTPS only. '
                             'Leave notify.url empty to run without a subscription (the journal is read every 2 s).')
        if scheme == 'https' and not (tls['cert'] and tls['key']):
            raise SystemExit('an https notify.url needs tls.cert and tls.key for this machine '
                             '(on the shore Pi: its own tls/server.crt and tls/server.key)')
    if cfg['api']['bind'] not in ('127.0.0.1', 'localhost', '::1'):
        if not cfg['api']['token_file']:
            raise SystemExit('api.bind is not loopback: set api.token_file so the API is not open to the network')
        if not (tls['cert'] and tls['key']):
            raise SystemExit('api.bind is not loopback: set tls.cert and tls.key so the token never travels in clear text')
    return cfg


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.URLError(f'redirect to {newurl} refused')  # as the core does


def make_opener(ca=''):
    """No system proxy (like trust_env=False in the core), no redirects, https checked against ca."""
    handlers = [urllib.request.ProxyHandler({}), NoRedirect()]
    if ca:
        context = ssl.create_default_context(cafile=ca)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        handlers.append(urllib.request.HTTPSHandler(context=context))
    return urllib.request.build_opener(*handlers)


def server_context(cert, key):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    return context


class TLSServer(ThreadingHTTPServer):
    """HTTPS with the handshake in the request thread, under a timeout, so a silent client
    cannot hold up the accept loop."""
    daemon_threads = True

    def __init__(self, address, handler, context):
        self.context = context
        super().__init__(address, handler)

    def finish_request(self, request, client_address):
        request.settimeout(10)
        conn = self.context.wrap_socket(request, server_side=True)
        try:
            self.RequestHandlerClass(conn, client_address, self)
        finally:
            conn.close()

    def shutdown_request(self, request):
        try:
            request.close()
        except OSError:
            pass

    def handle_error(self, request, client_address):
        log.debug('TLS connection from %s failed: %s', client_address[0], sys.exc_info()[1])


# ── time ──────────────────────────────────────────────────────────────────────

def ct_ms(value):
    """oneM2M timestamp 20261118T050102,123456 → epoch ms."""
    base, _, fraction = value.partition(',')
    t = datetime.strptime(base, '%Y%m%dT%H%M%S').replace(tzinfo=timezone.utc).timestamp()
    return round((t + (float('0.' + fraction) if fraction else 0)) * 1000, 3)


def ms_ct(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime('%Y%m%dT%H%M%S')


def now_ms():
    return round(time.time() * 1000, 3)


# ── oneM2M client (same headers and path rules as src/anchor/client.py) ───────

class OneM2MError(RuntimeError):
    def __init__(self, rsc, body, target):
        self.rsc, self.body, self.target = rsc, body, target
        super().__init__(f'oneM2M {rsc} at {target}: {json.dumps(body)[:200]}')


def resource(body):
    return next(iter(body.values()))


class CSE:
    def __init__(self, node, origin):
        self.url, self.root, self.csi = node['url'].rstrip('/'), node['root'], node['csi']
        self.rvi, self.timeout, self.origin = str(node.get('rvi', '3')), float(node.get('timeout', 3)), origin
        self.opener = make_opener(node.get('ca', ''))

    def request(self, method, target, body=None, ty=None, params=None):
        rsc, payload = self.exchange(method, target, body, ty, params)
        if not 2000 <= rsc < 3000:
            raise OneM2MError(rsc, payload, target)
        return payload

    def exchange(self, method, target, body=None, ty=None, params=None):
        """One request; returns (X-M2M-RSC, body) without raising on a oneM2M error code."""
        path = '/~' + target if target.startswith(self.csi + '/') else '/' + target.lstrip('/')
        url = self.url + quote(path, safe='/~:') + ('?' + urlencode(params) if params else '')
        headers = {'X-M2M-Origin': self.origin, 'X-M2M-RI': uuid.uuid4().hex,
                   'X-M2M-RVI': self.rvi, 'Accept': 'application/json'}
        data = None
        if body is not None:
            headers['Content-Type'] = 'application/json' + (f';ty={ty}' if ty else '')
            data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                raw, rsc = response.read(), response.headers.get('X-M2M-RSC')
        except urllib.error.HTTPError as exc:
            raw, rsc = exc.read(), exc.headers.get('X-M2M-RSC')
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {'error': raw[:300].decode(errors='replace')}
        return int(rsc or 5000), payload

    def get(self, target, params=None):
        return self.request('GET', target, params=params)

    def create(self, parent, ty, body):
        return self.request('POST', parent, body, ty)

    def update(self, target, body):
        return self.request('PUT', target, body)

    def discover(self, target, **filters):
        """Discovery with paging. ACME can repeat the final page, so stop on a page with nothing new."""
        refs, seen, offset = [], set(), 1
        while offset <= 1_000_000:
            page = self.get(target, {'fu': 1, 'lvl': 1, 'lim': 100, 'ofst': offset, **filters})
            fresh = [r for r in page.get('m2m:uril', []) if r not in seen]
            if not fresh:
                return refs
            refs.extend(fresh)
            seen.update(fresh)
            offset += 100
        raise RuntimeError(f'Discovery did not converge: {target}')


def describe(exc):
    if isinstance(exc, OneM2MError):
        hint = {4103: 'no permission (ACP)', 4004: 'not found', 4000: 'bad request'}.get(exc.rsc, '')
        return f'{exc.rsc} {hint}'.strip()
    if isinstance(exc, urllib.error.URLError):
        return f'unreachable ({exc.reason})'
    return f'{type(exc).__name__}: {exc}'[:200]


# ── store ─────────────────────────────────────────────────────────────────────

SCHEMA = '''
CREATE TABLE IF NOT EXISTS events(
  event_id TEXT PRIMARY KEY, generated_at_ms REAL NOT NULL, device_id TEXT, boot_id TEXT, seq INTEGER,
  kind TEXT, sensor TEXT, priority TEXT, envelope TEXT NOT NULL,
  coast_ri TEXT, coast_at_ms REAL, shore_ri TEXT, shore_at_ms REAL, rev INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS events_rev ON events(rev);
CREATE INDEX IF NOT EXISTS events_gen ON events(generated_at_ms);
CREATE TABLE IF NOT EXISTS commands(
  command_id TEXT PRIMARY KEY, body TEXT NOT NULL,
  coast_ri TEXT, coast_at_ms REAL, shore_ri TEXT, shore_at_ms REAL, rev INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS links(
  id INTEGER PRIMARY KEY AUTOINCREMENT, t_ms REAL NOT NULL, link TEXT NOT NULL, up INTEGER NOT NULL, source TEXT);
CREATE TABLE IF NOT EXISTS reach(
  id INTEGER PRIMARY KEY AUTOINCREMENT, t_ms REAL NOT NULL, role TEXT NOT NULL, ok INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS runs(
  run_id TEXT PRIMARY KEY, mode TEXT NOT NULL, start_ms REAL NOT NULL, end_ms REAL, created_ms REAL NOT NULL);
'''


class Store:
    def __init__(self, filename):
        self.filename = filename
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(filename, check_same_thread=False, isolation_level=None)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript(SCHEMA)
        self.rev = max(self.db.execute('SELECT COALESCE(MAX(rev), 0) FROM events').fetchone()[0],
                       self.db.execute('SELECT COALESCE(MAX(rev), 0) FROM commands').fetchone()[0])

    def reader(self):
        db = sqlite3.connect(self.filename, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def known(self, table, site):
        rows = self.db.execute(f'SELECT {site}_ri, {site}_at_ms FROM {table} WHERE {site}_ri IS NOT NULL').fetchall()
        return {r[0] for r in rows}, max((r[1] for r in rows), default=None)

    def _next(self):
        self.rev += 1
        return self.rev

    def put_event(self, site, ri, at_ms, env):
        """Returns 'new', 'copy' (the other site's copy was already known), 'same' or 'conflict'."""
        with self.lock:
            row = self.db.execute(f'SELECT envelope, {site}_ri FROM events WHERE event_id=?', (env['eventId'],)).fetchone()
            if row is None:
                seq = env.get('seq') if type(env.get('seq')) is int else None
                self.db.execute(
                    f'INSERT INTO events(event_id, generated_at_ms, device_id, boot_id, seq, kind, sensor, priority,'
                    f' envelope, {site}_ri, {site}_at_ms, rev) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (env['eventId'], round(env['generatedAt'] * 1000, 3), env['deviceId'], env['bootId'], seq,
                     env['kind'], env.get('sensor'), env['priority'], json.dumps(env, sort_keys=True),
                     ri, at_ms, self._next()))
                return 'new'
            if json.loads(row[0]) != env:
                return 'conflict'  # journals are immutable and hash-checked by the core; keep the first copy
            if row[1] is not None:
                return 'same'
            self.db.execute(f'UPDATE events SET {site}_ri=?, {site}_at_ms=?, rev=? WHERE event_id=?',
                            (ri, at_ms, self._next(), env['eventId']))
            return 'copy'

    def put_command(self, site, ri, at_ms, cmd):
        with self.lock:
            row = self.db.execute(f'SELECT {site}_ri FROM commands WHERE command_id=?', (cmd['commandId'],)).fetchone()
            if row is None:
                self.db.execute(f'INSERT INTO commands(command_id, body, {site}_ri, {site}_at_ms, rev) VALUES(?,?,?,?,?)',
                                (cmd['commandId'], json.dumps(cmd, sort_keys=True), ri, at_ms, self._next()))
            elif row[0] is None:
                self.db.execute(f'UPDATE commands SET {site}_ri=?, {site}_at_ms=?, rev=? WHERE command_id=?',
                                (ri, at_ms, self._next(), cmd['commandId']))

    def link_states(self):
        rows = self.db.execute('SELECT link, up FROM links WHERE id IN (SELECT MAX(id) FROM links GROUP BY link)')
        return {link: bool(up) for link, up in rows}

    def put_link(self, t_ms, link, up, source):
        with self.lock:
            self.db.execute('INSERT INTO links(t_ms, link, up, source) VALUES(?,?,?,?)', (t_ms, link, int(up), source))

    def put_reach(self, t_ms, role, ok):
        """When a core's control API became reachable or unreachable for the monitor."""
        with self.lock:
            self.db.execute('INSERT INTO reach(t_ms, role, ok) VALUES(?,?,?)', (t_ms, role, int(ok)))

    def current_run(self):
        row = self.db.execute('SELECT run_id, mode, start_ms, end_ms FROM runs WHERE end_ms IS NULL '
                              'ORDER BY start_ms DESC LIMIT 1').fetchone()
        return row and {'runId': row[0], 'mode': row[1], 'start': row[2], 'end': row[3]}

    def start_run(self, run_id, mode, start_ms):
        with self.lock:
            if self.db.execute('SELECT 1 FROM runs WHERE run_id=?', (run_id,)).fetchone():
                raise ValueError(f'run {run_id} already exists')
            t = now_ms()
            self.db.execute('UPDATE runs SET end_ms=? WHERE end_ms IS NULL', (t,))
            self.db.execute('INSERT INTO runs(run_id, mode, start_ms, end_ms, created_ms) VALUES(?,?,?,NULL,?)',
                            (run_id, mode, start_ms, t))

    def close_run(self):
        with self.lock:
            self.db.execute('UPDATE runs SET end_ms=? WHERE end_ms IS NULL', (now_ms(),))


# ── validation (minimal: the core validates the full envelope before writing) ─

def parse_con(cin):
    con = cin.get('con')
    value = json.loads(con) if isinstance(con, str) else con
    if not isinstance(value, dict):
        raise ValueError('record is not an object')
    return value


def check_event(env):
    if env.get('schemaVersion') != 2:
        raise ValueError('not an envelope v2')
    for key in ('eventId', 'vesselId', 'deviceId', 'bootId'):
        if not isinstance(env.get(key), str) or not env[key]:
            raise ValueError(f'missing {key}')
    if env.get('kind') not in KINDS or env.get('priority') not in ('critical', 'normal'):
        raise ValueError('invalid kind or priority')
    if type(env.get('generatedAt')) not in (int, float) or not isinstance(env.get('payload'), dict):
        raise ValueError('invalid generatedAt or payload')


def check_command(cmd):
    if not isinstance(cmd.get('commandId'), str) or not cmd['commandId']:
        raise ValueError('missing commandId')
    for key in ('issuedAtMs', 'expiresAtMs', 'durationMs', 'intervalMs'):
        if type(cmd.get(key)) is not int:
            raise ValueError(f'invalid {key}')


def core_status(c):
    """GET /status of a core's control API (anchor serve). The token is read from a file, never from the config."""
    token = Path(c['token_file']).read_text().strip() if c.get('token_file') else ''
    req = urllib.request.Request(c['url'].rstrip('/') + '/status',
                                 headers={'Authorization': 'Bearer ' + token} if token else {})
    with make_opener(c.get('ca', '')).open(req, timeout=float(c.get('timeout', 3))) as response:
        return json.loads(response.read())


# ── readers ───────────────────────────────────────────────────────────────────

class Source:
    """State of one thing the monitor reads, for /api/health."""

    def __init__(self, name, configured=True):
        self.name, self.configured = name, configured
        self.ok, self.at, self.error, self.records, self.last_ok = None, None, None, 0, None

    def success(self, added=0):
        self.ok, self.at, self.error, self.last_ok = True, now_ms(), None, now_ms()
        self.records += added

    def failure(self, exc):
        self.ok, self.at, self.error = False, now_ms(), describe(exc)

    def view(self):
        return {'configured': self.configured, 'ok': self.ok, 'at': self.at, 'lastOk': self.last_ok,
                'error': self.error, 'records': self.records}


class JournalReader:
    """Incremental reader of <root>/anchor/<name>: discovery with createdAfter, then a full pass now and then."""

    def __init__(self, cse, name, site, store, table, full_every, redact=True):
        self.cse, self.name, self.site, self.store, self.table = cse, name, site, store, table
        self.redact = redact
        self.known, self.high = store.known(table, site)
        self.full_every, self.last_full = full_every, 0.0
        self.source = Source(f'{site} anchor/{name}')
        self.rejected = []
        self.arrivals = collections.deque(maxlen=5000)  # local ms when a record first showed up at this site

    @property
    def path(self):
        return f'{self.cse.root}/anchor/{self.name}'

    def scan(self):
        try:
            full = self.high is None or time.monotonic() - self.last_full >= self.full_every
            filters = {'ty': 4, 'drt': 2}
            if not full:
                filters['cra'] = ms_ct(self.high - 10_000)  # overlap; already-known records are skipped
            added = 0
            for ref in self.cse.discover(self.path, **filters):
                if ref in self.known:
                    continue
                cin = resource(self.cse.get(ref))
                if cin.get('ty') != 4:
                    continue
                at = ct_ms(cin['ct'])
                try:
                    value = parse_con(cin)
                    if self.table == 'events':
                        check_event(value)
                        if self.redact and value.get('sensor') == 'position':
                            value['payload'] = {k: v for k, v in value['payload'].items() if k not in POSITION_FIELDS}
                            value['redacted'] = list(POSITION_FIELDS)
                        result = self.store.put_event(self.site, cin['ri'], at, value)
                        if result == 'conflict':
                            raise ValueError('differs from the copy at the other site')
                        if result in ('new', 'copy'):
                            self.arrivals.append(now_ms())
                    else:
                        check_command(value)
                        self.store.put_command(self.site, cin['ri'], at, value)
                    added += 1
                except (ValueError, KeyError, TypeError) as exc:
                    self.rejected = (self.rejected + [{'ri': cin.get('ri'), 'error': str(exc)[:200]}])[-20:]
                self.known.add(ref)
                self.high = at if self.high is None else max(self.high, at)
            if full:
                self.last_full = time.monotonic()
            self.source.success(added)
            return added
        except Exception as exc:  # network or CSE errors: keep going, report on /api/health
            self.source.failure(exc)
            return 0


class Monitor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.store = Store(cfg['db'])
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.shore = CSE(cfg['shore'], cfg['origin'])
        full, redact = cfg['full_scan_seconds'], cfg['redact_position']
        self.readers = {'shore': [JournalReader(self.shore, 'events', 'shore', self.store, 'events', full, redact),
                                  JournalReader(self.shore, 'commands', 'shore', self.store, 'commands', full)]}
        if cfg['edge']['url']:
            self.edge = CSE(cfg['edge'], cfg['origin'])
            self.readers['coast'] = [JournalReader(self.edge, 'events', 'coast', self.store, 'events', full, redact),
                                     JournalReader(self.edge, 'commands', 'coast', self.store, 'commands', full)]
        self.ae = Source('shore AE ' + cfg['ae_name'])
        self.subscription = Source('subscription', bool(cfg['notify']['url']))
        self.notifications = 0
        self.cores = {role: Source('core ' + role, bool(c['url'])) for role, c in cfg['cores'].items()}
        self.core_status = {}
        self.link_state = self.store.link_states()
        self.link_seen = {}
        self.reach = {}  # role → reachable at the last poll
        if not self.store.current_run():
            run = cfg['run']
            self.store.start_run(run['id'], run['mode'], now_ms() - run['history_minutes'] * 60_000)

    # oneM2M side --------------------------------------------------------------

    def ensure_ae(self):
        """The bootstrap creates AE 'dashboard' (origin CAnchorDashboard). Register it if it is missing."""
        path = f"{self.shore.root}/{self.cfg['ae_name']}"
        try:
            try:
                ae = resource(self.shore.get(path))
            except OneM2MError as exc:
                if exc.rsc != 4004:
                    raise
                ae = resource(self.shore.create(self.shore.root, 2, {'m2m:ae': {
                    'rn': self.cfg['ae_name'], 'api': 'Nanchor.v2', 'rr': False, 'srv': ['3', '4']}}))
            if ae.get('aei') != self.cfg['origin']:
                raise ValueError(f"AE {self.cfg['ae_name']} belongs to {ae.get('aei')}")
            self.ae.success()
        except Exception as exc:
            self.ae.failure(exc)

    def ensure_subscription(self):
        url = self.cfg['notify']['url']
        if not url:
            return
        parent = f'{self.shore.root}/anchor/events'
        path = f"{parent}/{self.cfg['notify']['sub_name']}"
        try:
            try:
                sub = resource(self.shore.get(path))
                if sub.get('nu') != [url]:
                    self.shore.update(path, {'m2m:sub': {'nu': [url]}})
            except OneM2MError as exc:
                if exc.rsc != 4004:
                    raise
                # net 3 = a record was created under anchor/events. The CSE verifies nu first.
                self.shore.create(parent, 23, {'m2m:sub': {'rn': self.cfg['notify']['sub_name'], 'nu': [url],
                                                           'enc': {'net': [3]}, 'nct': 1}})
            self.subscription.success()
        except Exception as exc:
            self.subscription.failure(exc)
            if isinstance(exc, OneM2MError) and exc.rsc == 4103:
                self.subscription.error = ('4103: CAnchorDashboard may not create the subscription '
                                           '(ACP needs acop 1 with acod chty [23] on anchor/events). Scanning only.')

    # core control API -----------------------------------------------------------

    def observe_link(self, link, peer_view, source):
        """links[peer] = {reachable, observedAt} as reported by the core that pushes over that link."""
        if not isinstance(peer_view, dict) or 'reachable' not in peer_view:
            return
        up = bool(peer_view['reachable'])
        at = round(float(peer_view.get('observedAt') or time.time()) * 1000, 3)
        self.link_seen[link] = {'up': up, 'observedAt': at, 'source': source}
        if self.link_state.get(link) != up:
            self.store.put_link(at, link, up, source)
            self.link_state[link] = up

    def poll_cores(self):
        for role, src in self.cores.items():
            if not src.configured:
                continue
            try:
                status = core_status(self.cfg['cores'][role])
                self.core_status[role] = {'at': now_ms(), 'delivery': status.get('delivery'),
                                          'links': status.get('links'), 'errors': (status.get('errors') or [])[-3:]}
                src.success()
            except Exception as exc:
                src.failure(exc)
                if self.reach.get(role) is not False:
                    self.store.put_reach(now_ms(), role, False)
                    self.reach[role] = False
                continue
            if self.reach.get(role) is not True:
                self.store.put_reach(now_ms(), role, True)
                self.reach[role] = True
            links = status.get('links') or {}
            if role == 'ship':
                self.observe_link('A', links.get('edge'), 'ship core')
            if role == 'edge':
                self.observe_link('B', links.get('shore'), 'edge core')

    def backlog(self):
        """Counts the shore cannot see by itself, from the cores' delivery receipts."""
        out = {}
        ship, edge = self.core_status.get('ship'), self.core_status.get('edge')
        if ship and ship.get('delivery') and self.cores['ship'].ok:
            d = ship['delivery']
            out.update(generated=d.get('accepted'), heldOnShip=max(0, d['accepted'] - d['coastCustody']),
                       shipAt=ship['at'])
        if edge and edge.get('delivery') and self.cores['edge'].ok:
            d = edge['delivery']
            out.update(atCoast=d.get('accepted'), waitingAtCoast=max(0, d['accepted'] - d['shoreConfirmed']),
                       edgeAt=edge['at'])
        elif ship and ship.get('delivery') and self.cores['ship'].ok:
            d = ship['delivery']  # the ship learns shore receipts later, through the coast
            out.update(waitingAtCoast=max(0, d['coastCustody'] - d['shoreConfirmed']))
        return out or None

    # loops ---------------------------------------------------------------------

    def _loop(self, every, fn):
        while not self.stop.is_set():
            fn()
            self.stop.wait(every)

    def _shore_loop(self):
        last_meta = float('-inf')  # not 0: monotonic() counts from boot, so a Pi started < 60 s ago would skip setup
        while not self.stop.is_set():
            if time.monotonic() - last_meta >= 60 or self.subscription.ok is False and time.monotonic() - last_meta >= 15:
                self.ensure_ae()
                self.ensure_subscription()
                last_meta = time.monotonic()
            for reader in self.readers['shore']:
                reader.scan()
            self.wake.wait(self.cfg['scan_seconds'])
            self.wake.clear()

    def start(self):
        threads = [threading.Thread(target=self._shore_loop, name='shore', daemon=True)]
        if 'coast' in self.readers:
            threads.append(threading.Thread(target=self._loop, name='coast', daemon=True,
                                            args=(self.cfg['scan_seconds'], lambda: [r.scan() for r in self.readers['coast']])))
        if any(s.configured for s in self.cores.values()):
            threads.append(threading.Thread(target=self._loop, name='cores', daemon=True,
                                            args=(self.cfg['core_seconds'], self.poll_cores)))
        for t in threads:
            t.start()

    # API views -----------------------------------------------------------------

    def health(self):
        readers = {r.source.name: {**r.source.view(), 'rejected': len(r.rejected)}
                   for group in self.readers.values() for r in group}
        return {'ok': bool(self.readers['shore'][0].source.ok), 'now': now_ms(),
                'shore': self.cfg['shore']['url'], 'edge': self.cfg['edge']['url'] or None,
                'ae': self.ae.view(), 'subscription': {**self.subscription.view(), 'notifications': self.notifications},
                'journals': readers, 'cores': {role: s.view() for role, s in self.cores.items()},
                'run': self.store.current_run()}

    def inferred_now(self):
        """Link state from arrivals alone, on the monitor's own clock (no comparison with CSE clocks).
        A: the coast keeps receiving → up; nothing for gap → down. B: records reach the coast but none
        reach the shore for gap → down; the shore keeps receiving → up. None when it cannot tell."""
        if 'coast' not in self.readers or not self.readers['coast'][0].source.ok:
            return {}
        gap, now = self.cfg['infer_gap_seconds'] * 1000, now_ms()
        coast, shore = self.readers['coast'][0].arrivals, self.readers['shore'][0].arrivals
        out = {}
        if coast:
            out['A'] = now - coast[-1] <= gap
        last_shore = shore[-1] if shore else float('-inf')
        waiting = next((t for t in coast if t > last_shore), None)
        if waiting is not None and now - waiting > gap:
            out['B'] = False
        elif shore and now - shore[-1] <= gap:
            out['B'] = True
        return out

    def live(self):
        links = {}
        inferred = self.inferred_now()
        for link, source in (('A', 'ship'), ('B', 'edge')):
            seen = self.link_seen.get(link)
            fresh = bool(seen) and self.cores[source].ok and now_ms() - seen['observedAt'] < 15_000
            if not fresh and link in inferred:  # the core is out of reach (an isolated ship cannot report)
                links[link] = {'known': True, 'up': inferred[link], 'observedAt': now_ms(), 'source': 'inferred'}
                continue
            links[link] = {'known': fresh, 'up': seen['up'] if seen else None,
                           'observedAt': seen['observedAt'] if seen else None,
                           'source': f'{source} core' if self.cores[source].configured else None}
        return {'links': links, 'backlog': self.backlog(),
                'sources': {'shore': self.readers['shore'][0].source.ok,
                            'coast': self.readers['coast'][0].source.ok if 'coast' in self.readers else None,
                            'subscription': self.subscription.ok}}


def infer_outages(rows, gap_ms, open_at=None, covered=lambda link, lo, hi: False):
    """Link outages read from the data itself, for when a core cannot report them (in the pool test
    the ship core is out of reach exactly while link A is down).

    rows: (generated_ms, coast_ms, shore_ms) per event. Times are the CSE creation times of each copy.
    A down: arrivals at the coast stop for more than gap_ms although the ship kept generating
            (some event was generated at least gap_ms before arrivals resumed).
    B down: arrivals at the shore stop for more than gap_ms although records sat at the coast
            for at least gap_ms.
    Down is the last arrival before the gap, up the first arrival after it, so both are accurate to
    about one transfer interval of the core; up means data flowed again, so it includes the core's
    own retry delay after the link came back. A link is left to its core wherever the monitor could
    reach that core for the whole interval (covered). open_at (the run is live): also report an
    outage still going on.
    """
    gen = sorted(r[0] for r in rows)
    coast = sorted(r[1] for r in rows if r[1] is not None)
    shore = sorted(r[2] for r in rows if r[2] is not None)
    if not coast:
        return []  # without the coast journal the two links cannot be told apart

    def between(values, lo, hi):  # any value in (lo, hi]
        i = bisect.bisect_right(values, lo)
        return i < len(values) and values[i] <= hi

    out = []
    for link, arrivals, before in (('A', coast, gen), ('B', shore, coast)):
        if not arrivals:
            continue
        for a, b in zip(arrivals, arrivals[1:]):
            if b - a > gap_ms and between(before, a, b - gap_ms) and not covered(link, a, b):
                out += [{'t': a, 'link': link, 'up': False, 'source': 'inferred'},
                        {'t': b, 'link': link, 'up': True, 'source': 'inferred'}]
        if open_at is not None and open_at.get(link) is False:
            out.append({'t': arrivals[-1], 'link': link, 'up': False, 'source': 'inferred'})
    return out


def delivered(row):
    env = json.loads(row['envelope'])
    return {'eventId': env['eventId'], 'vesselId': env['vesselId'], 'deviceId': env['deviceId'],
            'bootId': env['bootId'], 'seq': env['seq'] if type(env.get('seq')) is int else None,
            'generatedAt': row['generated_at_ms'], 'kind': env['kind'], 'sensor': env.get('sensor'),
            'priority': env['priority'], 'quality': env.get('quality'), 'payload': env['payload'],
            'coastAt': row['coast_at_ms'], 'shoreAt': row['shore_at_ms']}


def run_view(monitor, run_id=None, after=0):
    db = monitor.store.reader()
    try:
        if run_id:
            row = db.execute('SELECT run_id, mode, start_ms, end_ms FROM runs WHERE run_id=?', (run_id,)).fetchone()
        else:
            row = db.execute('SELECT run_id, mode, start_ms, end_ms FROM runs ORDER BY (end_ms IS NULL) DESC, '
                             'start_ms DESC LIMIT 1').fetchone()
        if row is None:
            raise KeyError(run_id)
        start, end = row['start_ms'], row['end_ms']
        upper = end if end is not None else float('inf')
        rev = max(db.execute('SELECT COALESCE(MAX(rev), 0) FROM events').fetchone()[0],
                  db.execute('SELECT COALESCE(MAX(rev), 0) FROM commands').fetchone()[0])
        events = [delivered(r) for r in db.execute(
            'SELECT * FROM events WHERE rev > ? AND generated_at_ms >= ? AND generated_at_ms <= ? ORDER BY generated_at_ms',
            (after, start, upper))]
        commands = []
        for r in db.execute('SELECT * FROM commands'):
            body = json.loads(r['body'])
            if start <= body['issuedAtMs'] <= upper:
                commands.append({**body, 'coastAt': r['coast_at_ms'], 'shoreAt': r['shore_at_ms']})
        commands.sort(key=lambda c: c['issuedAtMs'])
        # every change in the window, plus the last change before it so the state at the start is known
        links = [{'t': r['t_ms'], 'link': r['link'], 'up': bool(r['up']), 'source': r['source']} for r in db.execute(
            'SELECT * FROM links WHERE (t_ms >= ? AND t_ms <= ?) OR id IN '
            '(SELECT MAX(id) FROM links WHERE t_ms < ? GROUP BY link) ORDER BY t_ms, id', (start, upper, start))]
        times = db.execute('SELECT generated_at_ms, coast_at_ms, shore_at_ms FROM events '
                           'WHERE generated_at_ms >= ? AND generated_at_ms <= ?', (start, upper)).fetchall()
        live = monitor.live() if end is None else None
        ongoing = {k: v['up'] for k, v in live['links'].items() if v['source'] == 'inferred'} if live else None
        reach = {}
        for r in db.execute('SELECT t_ms, role, ok FROM reach ORDER BY t_ms, id'):
            reach.setdefault(r['role'], []).append((r['t_ms'], bool(r['ok'])))

        def covered(link, lo, hi):
            """The core of this link answered the monitor for the whole interval (its report is the truth)."""
            changes = reach.get({'A': 'ship', 'B': 'edge'}[link], [])
            before = [ok for t, ok in changes if t <= lo]
            return bool(before) and before[-1] and all(ok for t, ok in changes if lo < t <= hi)

        inferred = infer_outages(times, monitor.cfg['infer_gap_seconds'] * 1000, ongoing, covered)
        # While the monitor could not reach a core, that core's report is late: it shows the link state
        # only from the moment it answers again. Inside an inferred outage (and 15 s after it) the data wins.
        spans = []
        for c in inferred:
            if not c['up']:
                spans.append([c['link'], c['t'], float('inf')])
            elif spans and spans[-1][0] == c['link']:
                spans[-1][2] = c['t'] + 15_000
        links = sorted([c for c in links if not any(c['link'] == l and a <= c['t'] <= b for l, a, b in spans)]
                       + inferred, key=lambda c: c['t'])
        return {'runId': row['run_id'], 'mode': row['mode'], 'start': start, 'end': end, 'now': now_ms(),
                'rev': rev, 'after': after, 'events': events, 'commands': commands, 'links': links,
                'live': live}
    finally:
        db.close()


# ── HTTP: notifications from the CSE ─────────────────────────────────────────

def notify_handler(monitor):
    class Notify(BaseHTTPRequestHandler):
        server_version = 'anchor-shore-monitor'

        def log_message(self, fmt, *args):
            log.debug('notify %s', fmt % args)

        def do_POST(self):
            size = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(min(size, 1_000_000)) if size else b''
            try:
                sgn = json.loads(body).get('m2m:sgn', {}) if body else {}
            except ValueError:
                sgn = {}
            if sgn.get('sud'):
                monitor.subscription.ok = False  # deleted at the CSE: recreate on the next pass
            if not sgn.get('vrq'):
                monitor.notifications += 1
                monitor.wake.set()  # a hint only: the shore loop reads the new records from the CSE now
            self.send_response(200)
            self.send_header('X-M2M-RSC', '2000')
            if self.headers.get('X-M2M-RI'):
                self.send_header('X-M2M-RI', self.headers['X-M2M-RI'])
            self.send_header('Content-Length', '0')
            self.end_headers()

    return Notify


# ── HTTP: dashboard API ──────────────────────────────────────────────────────

def api_handler(monitor, token, dashboard_dir=''):
    class Api(BaseHTTPRequestHandler):
        server_version = 'anchor-shore-monitor'

        def log_message(self, fmt, *args):
            log.debug('api %s', fmt % args)

        def end_headers(self):
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Frame-Options', 'DENY')
            super().end_headers()

        def reply(self, code, value):
            data = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def allowed(self):
            """This machine itself always; anyone else only with the API token (Authorization: Bearer)."""
            if ipaddress.ip_address(self.client_address[0]).is_loopback:
                return True
            given = self.headers.get('Authorization', '').removeprefix('Bearer ').strip()
            return bool(token) and hmac.compare_digest(given.encode(), token.encode())

        def static(self, url):
            """The built dashboard. No data in it, so no token: the page asks for the token itself."""
            root = Path(dashboard_dir).resolve()
            if url.path == '/' and not url.query:
                self.send_response(302)
                self.send_header('Location', '/?live')
                self.send_header('Content-Length', '0')
                return self.end_headers()
            target = (root / (url.path.lstrip('/') or 'index.html')).resolve()
            if root not in target.parents and target != root or not target.is_file():
                return self.reply(404, {'error': 'not found'})
            data = target.read_bytes()
            kind = mimetypes.guess_type(target.name)[0] or 'application/octet-stream'
            self.send_response(200)
            self.send_header('Content-Type', kind + ('; charset=utf-8' if kind.startswith('text/') or kind.endswith('javascript') else ''))
            self.send_header('Cache-Control', 'no-cache' if target.name == 'index.html' else 'public, max-age=31536000, immutable')
            if target.suffix == '.html':
                self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                 "img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'")
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlparse(self.path)
            if not url.path.startswith('/api/'):
                if Path(dashboard_dir).is_dir():
                    return self.static(url)
                return self.reply(404, {'error': 'not found'})
            if not self.allowed():
                return self.reply(401, {'error': 'unauthorized'})
            q = {k: v[-1] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == '/api/health':
                    return self.reply(200, monitor.health())
                if url.path == '/api/runs':
                    db = monitor.store.reader()
                    try:
                        rows = db.execute('SELECT run_id, mode, start_ms, end_ms FROM runs ORDER BY start_ms').fetchall()
                    finally:
                        db.close()
                    return self.reply(200, [{'runId': r[0], 'mode': r[1], 'start': r[2], 'end': r[3]} for r in rows])
                if url.path == '/api/run':
                    return self.reply(200, run_view(monitor, q.get('run'), int(q.get('after', 0))))
                return self.reply(404, {'error': 'not found'})
            except KeyError:
                return self.reply(404, {'error': 'unknown run'})
            except ValueError as exc:
                return self.reply(400, {'error': str(exc)})

        def do_POST(self):
            if not self.allowed():
                return self.reply(401, {'error': 'unauthorized'})
            size = int(self.headers.get('Content-Length') or 0)
            try:
                body = json.loads(self.rfile.read(min(size, 10_000))) if size else {}
                if self.path == '/api/runs':
                    run_id, mode = str(body.get('runId', '')).strip(), body.get('mode', 'anchor')
                    if not run_id or len(run_id) > 64 or mode not in ('anchor', 'baseline'):
                        raise ValueError('runId (1..64 chars) and mode anchor|baseline are required')
                    monitor.store.start_run(run_id, mode, now_ms())
                    return self.reply(201, monitor.store.current_run())
                if self.path == '/api/runs/close':
                    monitor.store.close_run()
                    return self.reply(200, {'closed': True})
                return self.reply(404, {'error': 'not found'})
            except ValueError as exc:
                return self.reply(400, {'error': str(exc)})

    return Api


# ── commands ─────────────────────────────────────────────────────────────────

def check(cfg):
    """Read-only check of every configured endpoint and permission. Nothing is written."""
    origin = cfg['origin']
    rows = []

    def probe(label, fn):
        try:
            rows.append(('ok', label, fn()))
        except Exception as exc:
            rows.append(('--', label, describe(exc)))

    if not cfg['shore']['url']:
        print('shore.url is empty: fill in the shore CSE address in the config first.')
        return 1
    for site in ('shore', 'edge'):
        if not cfg[site]['url']:
            rows.append(('  ', f'{site} CSE', 'not configured'))
            continue
        cse = CSE(cfg[site], origin)
        probe(f'{site} CSE {cse.url} ({cse.csi})', lambda: resource(cse.get(cse.root)).get('ri'))
        for name in ('events', 'commands'):
            probe(f'{site} {cse.root}/anchor/{name} as {origin} (R Di)',
                  lambda n=name: f"{len(cse.discover(f'{cse.root}/anchor/{n}', ty=4, drt=2))} records")
        if site == 'shore':
            probe(f"shore AE {cse.root}/{cfg['ae_name']}",
                  lambda: resource(cse.get(f"{cse.root}/{cfg['ae_name']}")).get('aei'))
            sub = f"{cse.root}/anchor/events/{cfg['notify']['sub_name']}"
            if not cfg['notify']['url']:
                rows.append(('  ', 'shore subscription', 'off (notify.url empty): the journal is read every 2 s'))
            else:
                try:
                    rsc, body = cse.exchange('GET', sub)
                except Exception as exc:
                    rsc, body = None, describe(exc)
                rows.append(('--', f'shore subscription {sub}', body)) if rsc is None else rows.append(('ok', f'shore subscription {sub}', resource(body).get('nu')) if rsc == 2000 else
                            ('  ', f'shore subscription {sub}', 'not made yet: serve creates it') if rsc == 4004 else
                            ('--', f'shore subscription {sub}', describe(OneM2MError(rsc, body, sub))))
    for role, c in cfg['cores'].items():
        if not c['url']:
            rows.append(('  ', f'{role} core API', 'not configured'))
            continue
        probe(f"{role} core API {c['url']}/status", lambda c=c: 'links ' + json.dumps(core_status(c).get('links')))
    width = max(len(r[1]) for r in rows)
    for mark, label, value in rows:
        print(f'[{mark}] {label.ljust(width)}  {value}')
    return 0 if rows[0][0] == 'ok' else 1


def serve(cfg):
    if not cfg['shore']['url']:
        raise SystemExit('shore.url is empty: fill in the shore CSE address in the config first.')
    monitor = Monitor(cfg)
    token = Path(cfg['api']['token_file']).read_text().strip() if cfg['api']['token_file'] else ''
    tls = cfg['tls']
    api_address = (cfg['api']['bind'], cfg['api']['port'])
    if cfg['api']['bind'] in ('127.0.0.1', 'localhost', '::1'):
        api = ThreadingHTTPServer(api_address, api_handler(monitor, token, cfg['dashboard_dir']))  # loopback
    else:
        api = TLSServer(api_address, api_handler(monitor, token, cfg['dashboard_dir']),
                        server_context(tls['cert'], tls['key']))
    servers = [api]
    if cfg['notify']['url']:
        address, handler = (cfg['notify']['bind'], cfg['notify']['port']), notify_handler(monitor)
        servers.append(TLSServer(address, handler, server_context(tls['cert'], tls['key']))
                       if cfg['notify']['url'].startswith('https://') else ThreadingHTTPServer(address, handler))
    for s in servers:
        s.daemon_threads = True
        threading.Thread(target=s.serve_forever, daemon=True).start()
    monitor.start()
    log.info('shore CSE %s · API %s://%s:%s/api/run · notifications %s', cfg['shore']['url'],
             'https' if isinstance(api, TLSServer) else 'http', cfg['api']['bind'], cfg['api']['port'],
             cfg['notify']['url'] or 'off (scan only)')
    try:
        while True:
            time.sleep(10)
            h = monitor.health()
            log.info('events at shore %s · subscription %s · notifications %s',
                     h['journals']['shore anchor/events']['records'], h['subscription']['ok'], monitor.notifications)
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop.set()
        for s in servers:
            s.shutdown()
    return 0


def init_token(cfg):
    """Create the API token file (mode 600). Never overwrites, never prints the token."""
    path = cfg['api']['token_file']
    if not path:
        raise SystemExit('set api.token_file in the config first')
    path = Path(path)
    if path.exists():
        print(f'{path} already exists; left unchanged')
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(secrets.token_urlsafe(32) + '\n')
    print(f'created {path} (mode 600). Show it with: cat {path}')
    return 0


def runs_command(cfg, args):
    """Start, close or list test runs on this machine, straight in the database (no HTTP, no token)."""
    store = Store(cfg['db'])
    if args.command == 'run-start':
        if not args.id or args.mode not in ('anchor', 'baseline'):
            raise SystemExit('run-start needs --id <name> and --mode anchor|baseline')
        store.start_run(args.id, args.mode, now_ms())
        print('started', store.current_run())
    elif args.command == 'run-close':
        store.close_run()
        print('closed; open run now:', store.current_run())
    for r in store.db.execute('SELECT run_id, mode, start_ms, end_ms FROM runs ORDER BY start_ms'):
        when = lambda ms: datetime.fromtimestamp(ms / 1000).strftime('%m-%d %H:%M:%S') if ms else 'open'
        print(f'  {r[0]:24} {r[1]:8} {when(r[2])} → {when(r[3])}')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='ANCHOR Shore Monitor AE (shore journal → SQLite → dashboard API)')
    parser.add_argument('--config', required=True)
    parser.add_argument('-v', '--verbose', action='store_true')
    parser.add_argument('command', choices=('serve', 'check', 'init-token', 'run-start', 'run-close', 'runs'))
    parser.add_argument('--id', help='run-start: name of the test run, e.g. pool-anchor-01')
    parser.add_argument('--mode', default='anchor', help='run-start: anchor or baseline')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    cfg = load_config(args.config)
    if args.command == 'check':
        return check(cfg)
    if args.command == 'init-token':
        return init_token(cfg)
    if args.command in ('run-start', 'run-close', 'runs'):
        return runs_command(cfg, args)
    return serve(cfg)


if __name__ == '__main__':
    sys.exit(main())
