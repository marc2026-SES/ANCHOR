#!/usr/bin/env python3
"""ANCHOR ship-to-shore relay: notification in, one create out, SQLite store-and-forward.

  ship MN-CSE  --SUB notify (HTTPS, whole CIN)-->  this relay  --HTTPS CIN create-->  shore IN-CSE
                                                     |
                                                     +-- SQLite queue (survives link outages and
                                                         power loss, FIFO replay)

- The relay subscribes to the six sensor containers (subscription `anchor-core`, nct=1) and uses
  the CIN carried by each notification. It never lists or re-reads the ship CSE.
- Each hardware CIN is wrapped in a versioned envelope (schemaVersion 2) and created in
  <shore>/anchor/events under a deterministic name, so a repeated send is a 4105 conflict
  instead of a duplicate. One request per record.
- Records stay in the SQLite queue until the shore has stored them.

It needs, on the CSEs it talks to (created by the deployment, not by the relay):
  ship : read + subscribe on <device>/<sensor> for the relay originator
  shore: create on anchor/events for the relay originator

Run from the package root with the deployment venv:
  .venv/bin/python hardware/ship-gateway-pi5/sync/anchor_relay.py --config deploy/ship/relay.json
  ... --check     read-only: wrap the newest CIN of every sensor and probe the shore, then exit
"""
import argparse
import hashlib
import json
import math
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'src'))

from anchor.client import Client, OneM2MError, canonical, resource  # noqa: E402
from anchor.config import Endpoint  # noqa: E402
from anchor.tls import require_https, server_context  # noqa: E402

SENSORS = {'bridge': ('attitude', 'position'), 'engine': ('vibration', 'gas'), 'cargo': ('climate', 'hatch')}
UNITS = {'attitude': {'roll': 'deg', 'pitch': 'deg', 'gx': 'deg/s', 'gy': 'deg/s', 'gz': 'deg/s',
                      'ax': 'g', 'ay': 'g', 'az': 'g'},
         'position': {'lat': 'deg', 'lon': 'deg', 'sog_kn': 'kn', 'cog': 'deg'},
         'vibration': {'vib_rms_g': 'g', 'vib_peak_g': 'g', 'mean_g': 'g'},
         'gas': {'mq2_v': 'V'}, 'climate': {'temp_c': 'degC', 'rh': '%'}, 'hatch': {'open_s': 's'}}
BATCH = 50          # queued records taken per forwarding pass
RETRY_MAX_S = 5.0   # longest pause between attempts while the shore is unreachable


def log(*parts):
    print(time.strftime('%H:%M:%S'), *parts, flush=True)


# ---- envelope (schemaVersion 2, the record format shore readers already consume) -----------------
def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def record_name(identity):
    return 'e-' + hashlib.sha256(identity.encode()).hexdigest()[:32]


def ct_time(value):
    base, _, fraction = value.partition(',')
    stamp = datetime.strptime(base, '%Y%m%dT%H%M%S').replace(tzinfo=timezone.utc).timestamp()
    return stamp + (float('0.' + fraction) if fraction else 0)


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def envelope(cin, device, sensor, vessel_id, csi):
    """Hardware CIN -> immutable transport record. The hardware payload is kept intact."""
    payload = json.loads(cin['con']) if isinstance(cin['con'], str) else cin['con']
    if not isinstance(payload, dict):
        raise ValueError('Hardware con must be an object')
    if payload.get('deviceId', device) != device or payload.get('vesselId', vessel_id) != vessel_id:
        raise ValueError('Hardware identity does not match its source')
    if sensor == 'attitude' and not all(number(payload.get(k)) for k in ('roll', 'pitch')):
        raise ValueError('Invalid attitude')
    seq = payload.get('seq')
    if type(seq) is not int or seq < 0:
        raise ValueError('Invalid hardware sequence')
    boot = payload.get('bootId')
    legacy = not boot
    if legacy:
        # Old firmware has no reboot identity. Never infer loss-free streams from its seq.
        boot = 'legacy-' + digest([csi, cin['ri'], cin['ct']])[:24]
    if not isinstance(boot, str) or ':' in boot:
        raise ValueError('Invalid bootId')
    event_id = f'{vessel_id}:{device}:{boot}:{seq}'
    if payload.get('eventId', event_id) != event_id:
        raise ValueError('Invalid eventId')
    if 'uptimeMs' in payload and (not number(payload['uptimeMs']) or payload['uptimeMs'] < 0):
        raise ValueError('Invalid uptime')
    ts = payload.get('ts', 0)
    device_time = number(ts) and ts > 0
    quality = {'identity': 'legacy-resource' if legacy else 'device-boot-sequence',
               'time': 'device' if device_time else 'cse', 'droppedBefore': payload.get('droppedBefore', 0)}
    if sensor == 'position':
        quality['validFix'] = payload.get('fix') is True and payload.get('gps') == 'ok'
    elif sensor == 'climate':
        quality['sensorOk'] = payload.get('ok') is True
    elif sensor == 'gas':
        quality.update(warmed=payload.get('warm') is True, calibratedConcentration=False)
    event = {'schemaVersion': 2, 'eventId': event_id, 'vesselId': vessel_id, 'deviceId': device,
             'bootId': boot, 'seq': seq, 'generatedAt': ts / 1000 if device_time else ct_time(cin['ct']),
             'kind': 'measurement', 'sensor': sensor, 'priority': 'normal',
             'units': UNITS.get(sensor, {}), 'quality': quality,
             'source': {'csi': csi, 'ri': cin['ri'], 'ct': cin['ct']}, 'payload': payload}
    canonical(event)  # rejects NaN/Infinity before anything is queued
    return event


# ---- configuration -------------------------------------------------------------------------------
class Config:
    """deploy/ship/relay.json. Relative paths are resolved against the config file."""

    def __init__(self, filename):
        path = Path(filename).resolve()
        data = json.loads(path.read_text())

        def located(value):
            return str(value if Path(value).is_absolute() else (path.parent / value).resolve())

        self.vessel_id = data.get('vessel_id', 'ship-001')
        self.origin = data.get('origin', 'CAnchorShip')
        self.bind, self.port = data.get('bind', '127.0.0.1'), int(data.get('port', 8790))
        self.notify_url = require_https(data.get('notify_url', f'https://127.0.0.1:{self.port}/notify'))
        self.tls_cert, self.tls_key = located(data['tls_cert']), located(data['tls_key'])
        self.queue = located(data.get('queue', '../../.runtime-ship/relay-queue.db'))
        self.status_tokens = [located(f) for f in data.get('status_token_files', [])]
        self.nodes = {}
        for name in ('ship', 'shore'):
            node = dict(data['nodes'][name])
            if isinstance(node.get('ca'), str):
                node['ca'] = located(node['ca'])
            self.nodes[name] = Endpoint(**node)
            require_https(self.nodes[name].url)
        if not self.vessel_id or ':' in self.vessel_id:
            raise ValueError('vessel_id must be nonempty and contain no colon')


class Queue:
    """FIFO of records waiting for the shore. The unique name makes a repeated notification harmless."""

    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS q (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                        'rn TEXT UNIQUE NOT NULL, body TEXT NOT NULL, queued REAL NOT NULL)')

    def put(self, rn, body):
        with self.lock:
            return self.db.execute('INSERT OR IGNORE INTO q (rn, body, queued) VALUES (?, ?, ?)',
                                   (rn, body, time.time())).rowcount

    def head(self, limit):
        with self.lock:
            return self.db.execute('SELECT id, rn, body FROM q ORDER BY id LIMIT ?', (limit,)).fetchall()

    def done(self, row_id):
        with self.lock:
            self.db.execute('DELETE FROM q WHERE id = ?', (row_id,))

    def size(self):
        with self.lock:
            return self.db.execute('SELECT COUNT(*) FROM q').fetchone()[0]


class Relay:
    def __init__(self, config, queue_path):
        self.config = config
        self.ship = Client(config.nodes['ship'], config.origin)
        self.shore = Client(config.nodes['shore'], config.origin)
        # This worker has exactly one permission key for its lifetime:
        # shore CSE + configured origin + anchor/events + CREATE + CIN (4).
        self.events_parent = self.shore.root + '/anchor/events'
        self.events_create_confirmed = False
        self.queue = Queue(queue_path)
        self.sources = {}  # container ri -> (device, sensor)
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.stats = {'received': 0, 'queued': 0, 'rejected': 0, 'forwarded': 0, 'duplicates': 0}
        self.link = {'reachable': None, 'observedAt': None}
        self.last_error = None
        self.last_received = None

    # ---- ship side -------------------------------------------------------------------------
    def discover(self):
        """Sensor containers by resource id; keep the subscription the notifications arrive through."""
        found = {}
        for device, sensors in SENSORS.items():
            for sensor in sensors:
                path = f'{self.ship.root}/{device}/{sensor}'
                cnt = resource(self.ship.get(path))
                found[cnt['ri']] = (device, sensor)
                self.ship.ensure(path, 23, {'m2m:sub': {'rn': 'anchor-core', 'nu': [self.config.notify_url],
                                                        'enc': {'net': [3]}, 'nct': 1}})
        self.sources = found
        return found

    def wrap(self, cin, device, sensor):
        return envelope(cin, device, sensor, self.config.vessel_id, self.ship.endpoint.csi)

    def accept(self, cin):
        """One notified CIN -> queue. Returns True when it was newly queued."""
        with self.lock:
            self.stats['received'] += 1
            self.last_received = time.time()
        source = self.sources.get(cin.get('pi'))
        if source is None:
            return False
        try:
            event = self.wrap(cin, *source)
        except (ValueError, KeyError, TypeError) as exc:
            with self.lock:
                self.stats['rejected'] += 1
                self.last_error = f'rejected {cin.get("ri")}: {exc}'[:300]
            return False
        added = self.queue.put(record_name(event['eventId']), canonical(event))
        if added:
            with self.lock:
                self.stats['queued'] += 1
            self.wake.set()
        return bool(added)

    # ---- shore side ------------------------------------------------------------------------
    def forward_record(self, rn, body):
        try:
            self.shore.create(self.events_parent, 4, {'m2m:cin': {'rn': rn, 'cnf': 'application/json:0', 'con': body}})
        except OneM2MError as exc:
            if exc.http_status in (401, 403) or exc.rsc in (4103, 4107, 5105):
                self.events_create_confirmed = False
            elif exc.rsc == 4105:
                return 'duplicates'
            raise
        if not self.events_create_confirmed:
            self.events_create_confirmed = True
        return 'forwarded'

    def forward_loop(self):
        pause = 0.5
        while not self.stop.is_set():
            rows = self.queue.head(BATCH)
            if not rows:
                self.wake.wait(1.0)
                self.wake.clear()
                continue
            for row_id, rn, body in rows:
                if self.stop.is_set():
                    return
                try:
                    key = self.forward_record(rn, body)
                    self.queue.done(row_id)
                    with self.lock:
                        self.stats[key] += 1
                        self.link = {'reachable': True, 'observedAt': time.time()}
                    pause = 0.5
                except Exception as exc:
                    with self.lock:
                        was = self.link['reachable']
                        self.link = {'reachable': False, 'observedAt': time.time()}
                        self.last_error = f'forward: {exc}'[:300]
                    if was is not False:
                        log('shore unreachable, keeping records queued:', str(exc)[:160])
                    self.stop.wait(pause)
                    pause = min(RETRY_MAX_S, pause * 2)
                    break

    def status(self):
        with self.lock:
            stats, link = dict(self.stats), dict(self.link)
            error, received = self.last_error, self.last_received
        backlog = self.queue.size()
        return {'role': 'ship', 'mode': 'relay', 'ready': bool(self.sources), 'lastReceivedAt': received,
                'links': {'shore': {**link, 'pendingAtPeer': backlog}},
                'delivery': {'accepted': stats['queued'], 'shoreStored': stats['forwarded'] + stats['duplicates'],
                             'pending': backlog},
                'relay': stats, 'lastError': error}


def make_server(relay, tokens):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def send(self, status, value):
            data = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-M2M-RSC', '2000' if status < 300 else '4000')
            self.send_header('X-M2M-RI', self.headers.get('X-M2M-RI', ''))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == '/health':
                return self.send(200 if relay.sources else 503, {'ready': bool(relay.sources), 'role': 'ship'})
            if self.path == '/status':
                given = self.headers.get('Authorization', '').removeprefix('Bearer ').strip()
                if not given or given not in tokens:
                    return self.send(401, {'error': 'API token required'})
                return self.send(200, relay.status())
            self.send(404, {'error': 'Not found'})

        def do_POST(self):
            try:
                size = int(self.headers.get('Content-Length', 0))
                if not 0 <= size <= 1_000_000:
                    return self.send(413, {'error': 'Invalid body size'})
                body = json.loads(self.rfile.read(size) or b'{}')
            except ValueError:
                return self.send(400, {'error': 'Invalid JSON'})
            if self.path != '/notify':
                return self.send(404, {'error': 'Not found'})
            sgn = body.get('m2m:sgn', {}) if isinstance(body, dict) else {}
            cin = (sgn.get('nev') or {}).get('rep', {}).get('m2m:cin') if not sgn.get('vrq') else None
            if isinstance(cin, dict):
                relay.accept(cin)
            self.send(200, {})

    httpd = ThreadingHTTPServer((relay.config.bind, relay.config.port), Handler)
    httpd.daemon_threads = True
    # Handshake in the request thread: an idle preconnect must not block accept().
    httpd.socket = server_context(relay.config.tls_cert, relay.config.tls_key).wrap_socket(
        httpd.socket, server_side=True, do_handshake_on_connect=False)
    return httpd


def read_tokens(config):
    return {Path(name).read_text().strip() for name in config.status_tokens if Path(name).is_file()} - {''}


def check(relay):
    """Read-only self test: nothing is queued, created or subscribed."""
    for device, sensors in SENSORS.items():
        for sensor in sensors:
            try:
                cin = resource(relay.ship.get(f'{relay.ship.root}/{device}/{sensor}/la'))
                event = relay.wrap(cin, device, sensor)
                print(f'  {device}/{sensor}: ok  {event["eventId"]}  -> {record_name(event["eventId"])}')
            except Exception as exc:
                print(f'  {device}/{sensor}: {exc}')
    try:
        cnt = resource(relay.shore.get(f'{relay.shore.root}/anchor/events'))
        print(f'  shore anchor/events reachable as {relay.shore.origin}: cni={cnt.get("cni")}')
    except Exception as exc:
        print(f'  shore: {exc}')


def main(argv=None):
    parser = argparse.ArgumentParser(description='ANCHOR ship-to-shore relay')
    parser.add_argument('--config', required=True, help='deploy/ship/relay.json')
    parser.add_argument('--check', action='store_true', help='read-only self test, then exit')
    args = parser.parse_args(argv)
    config = Config(args.config)
    relay = Relay(config, ':memory:' if args.check else config.queue)
    if args.check:
        return check(relay)

    httpd = make_server(relay, read_tokens(config))
    threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': 0.2}, daemon=True).start()
    threading.Thread(target=relay.forward_loop, daemon=True, name='forward').start()

    def shutdown(*_):
        relay.stop.set()
        relay.wake.set()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log(f'relay listening on :{config.port}, queue {config.queue} ({relay.queue.size()} waiting)')
    announced = False
    while not relay.stop.is_set():
        try:
            found = relay.discover()  # re-checked every minute: a reset CSE gets its subscriptions back
            if not announced:
                log(f'watching {len(found)} sensor containers, forwarding to {config.nodes["shore"].url}')
                announced = True
        except Exception as exc:
            log('ship CSE not ready:', str(exc)[:200])
        relay.stop.wait(60 if relay.sources else 3)
    httpd.shutdown()
    log('stopped,', relay.queue.size(), 'records left in the queue')


if __name__ == '__main__':
    main()
