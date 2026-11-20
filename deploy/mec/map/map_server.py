#!/usr/bin/env python3
"""Live map of the MEC Sandbox GIS + simulated disasters (typhoon, tsunami).

Read (no auth, CORS open):
  GET  /                    map page
  GET  /api/geodata         {"sandbox", "ts", "ues": [...], "poas": [...], "hazards": [...]}
  GET  /api/hazards         current hazards with geometry and affected base stations / ships
Write (no auth; Tailscale only):
  POST   /api/hazards       create a hazard (see make_hazard for fields)
  DELETE /api/hazards/{id}  remove one hazard ("all" removes every hazard)

Hazards degrade the network of base stations (POAs) inside them through the sandbox
NETWORK-CHARACTERISTICS-UPDATE event and restore the scenario values when they leave.
The sandbox namespace is discovered via kubectl, so a recreated sandbox is picked up.
"""
import calendar, json, math, os, secrets, ssl, subprocess, threading, time, urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import land

PORT = 30793
SANDBOX = os.environ.get('MAP_SANDBOX', 'sbx3btt489')
HERE = Path(__file__).resolve().parent
HTML = HERE / 'map.html'
STATE = HERE / 'hazards.json'
LOG = HERE / 'map.log'

_lock = threading.RLock()
_sbx = {'ns': None, 'gis': None, 'ctrl': None, 'at': 0}
_cache = {'at': 0, 'ues': [], 'poas': []}
_base_net = {}      # poa -> scenario netChar
_applied = {}       # poa -> {'level', 'netChar'} for POAs a hazard currently degrades
_pushed = {}        # poa -> netChar last pushed (empty after (re)load => every POA is pushed once)
hazards = {}


def log(msg):
    with open(LOG, 'a') as f:
        f.write(time.strftime('%H:%M:%S ') + msg + '\n')


# ---------------------------------------------------------------- sandbox access
def discover(force=False):
    if not force and _sbx['gis'] and time.time() - _sbx['at'] < 60:
        return
    out = subprocess.run(['kubectl', 'get', 'svc', '-A', '-o', 'json'], capture_output=True, text=True, timeout=20).stdout
    items = [i for i in json.loads(out or '{}').get('items', []) if i['metadata']['namespace'].startswith('sbx')]
    by_ns = {}
    for i in items:
        if i['metadata']['name'] in ('meep-gis-engine', 'meep-sandbox-ctrl'):
            by_ns.setdefault(i['metadata']['namespace'], {})[i['metadata']['name']] = (i['spec']['clusterIP'], i['metadata']['creationTimestamp'])
    full = {ns: v for ns, v in by_ns.items() if len(v) == 2}
    if not full:
        raise RuntimeError('no running sandbox (meep-gis-engine not found)')
    # Pinned to the ANCHOR sandbox; web logins create other sandboxes that must not be picked.
    ns = SANDBOX if SANDBOX in full else None
    if ns is None:
        raise RuntimeError(f'sandbox {SANDBOX} not running')
    if ns != _sbx['ns']:
        _base_net.clear(); _applied.clear(); _pushed.clear()
    _sbx.update(ns=ns, gis=full[ns]['meep-gis-engine'][0], ctrl=full[ns]['meep-sandbox-ctrl'][0], at=time.time())


# Read-only core status relay: the admin tokens stay on this server; monitors call
# GET /core/{ship,edge,shore}/status without any token and get only non-sensitive fields.
CORE_HOST = os.environ.get('CORE_HOST', '172.19.30.16')  # LAN IP: Calico eBPF serves NodePorts on data interfaces only
CORE_PORTS = {'ship': 30792, 'edge': 30790, 'edge-b': 30794, 'shore': None}  # shore = real Pi (see core_status)
# Sandbox cores and CSEs are HTTPS, signed by the sandbox CA (deploy/mec/make_certs.sh)
SANDBOX_CA = os.environ.get('SANDBOX_CA', str(Path.home() / 'anchor-secrets/sandbox-tls/ca.crt'))
_ssl = ssl.create_default_context(cafile=SANDBOX_CA) if os.path.exists(SANDBOX_CA) else None
CORE_TOKENS = Path(os.environ.get('ANCHOR_TOKENS', str(HERE.parent / 'tokens.json')))  # deploy/mec/tokens.json (gitignored)
STATUS_FIELDS = ('role', 'mode', 'ready', 'lastScanAt', 'lastReceivedAt', 'delivery', 'relay', 'sequenceGaps', 'links',
                 'pollingChannelReceived', 'notificationRequests', 'handover', 'cse', 'forwardedThroughStateTag',
                 'lastShoreStoreAt', 'lastError')


REAL_SHORE = os.environ.get('REAL_SHORE', 'https://100.98.242.10:8443/cse-in')


def core_status(role):
    if role == 'shore':
        # The shore is the real Pi 4 (no status token for us): report it as the two coastal relays see it.
        coasts = {}
        for name in ('edge', 'edge-b'):
            try:
                e = core_status(name)
                coasts[name] = {'reachable': ((e.get('links') or {}).get('shore') or {}).get('reachable'),
                                'lastShoreStoreAt': e.get('lastShoreStoreAt'),
                                'forwardedThroughStateTag': e.get('forwardedThroughStateTag')}
            except Exception as exc:
                coasts[name] = {'error': str(exc)[:120]}
        return {'role': 'shore', 'source': 'real shore Pi, as seen by the coastal relays', 'cse': REAL_SHORE, 'coasts': coasts}
    token = json.loads(CORE_TOKENS.read_text())['edge' if role == 'edge-b' else role]
    req = urllib.request.Request(f'https://{CORE_HOST}:{CORE_PORTS[role]}/status', headers={'Authorization': 'Bearer ' + token})
    with urllib.request.urlopen(req, timeout=8, context=_ssl) as r:
        d = json.load(r)
    links = {k: {f: v.get(f) for f in ('reachable', 'observedAt', 'pendingAtPeer', 'node')} for k, v in (d.get('links') or {}).items()}
    return {**{f: d.get(f) for f in STATUS_FIELDS}, 'links': links, 'errorCount': len(d.get('errors') or [])}


# fleet-ai collision warnings: latest CIN of cse-edge/fleetAI/conflicts (written by fleet-ai)
CONFLICTS_URL = os.environ.get('CONFLICTS_URL', f'https://{CORE_HOST}:30080/cse-edge/fleetAI/conflicts/la')
CONFLICT_MAX_AGE = float(os.environ.get('CONFLICT_MAX_AGE', '120'))  # seconds; older warnings are hidden
_conf = {'at': 0, 'data': None}


def _pair(item):
    for a, b in (('a', 'b'), ('shipA', 'shipB'), ('ship1', 'ship2'), ('from', 'to')):
        if isinstance(item.get(a), str) and isinstance(item.get(b), str):
            return item[a], item[b]
    ships = item.get('ships') or item.get('pair')
    if isinstance(ships, list) and len(ships) >= 2 and all(isinstance(x, str) for x in ships[:2]):
        return ships[0], ships[1]
    return None


def conflicts():
    """[{a, b, ...original fields}] from conflicts/la, or [] when missing/stale."""
    if time.time() - _conf['at'] < 2:
        return _conf['data']
    out = {'items': [], 'ct': None, 'ageSec': None, 'error': None}
    try:
        req = urllib.request.Request(CONFLICTS_URL, headers={'X-M2M-Origin': 'CAdmin', 'X-M2M-RI': 'map', 'X-M2M-RVI': '3', 'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=4, context=_ssl) as r:
            cin = json.load(r)['m2m:cin']
        con = json.loads(cin['con']) if isinstance(cin['con'], str) else cin['con']
        ct = calendar.timegm(time.strptime(cin['ct'][:15], '%Y%m%dT%H%M%S'))
        out.update(ct=cin['ct'], ageSec=round(time.time() - ct))
        if isinstance(con, dict):
            raw = next((con[k] for k in ('conflicts', 'pairs', 'warnings', 'alerts') if isinstance(con.get(k), list)), [con])
        else:
            raw = con if isinstance(con, list) else []
        if out['ageSec'] <= CONFLICT_MAX_AGE:
            for item in raw:
                if isinstance(item, dict) and (pr := _pair(item)):
                    out['items'].append({**item, 'a': pr[0], 'b': pr[1]})
    except urllib.error.HTTPError as e:
        out['error'] = None if e.code == 404 else f'HTTP {e.code}'
    except Exception as e:
        out['error'] = str(e)
    _conf.update(at=time.time(), data=out)
    return out


def platform_ctrl():
    if not _sbx.get('pfm'):
        _sbx['pfm'] = subprocess.run(['kubectl', '-n', 'default', 'get', 'svc', 'meep-platform-ctrl', '-o', 'jsonpath={.spec.clusterIP}'],
                                     capture_output=True, text=True, timeout=15).stdout.strip()
    return _sbx['pfm']


def http_json(url, body=None, method=None, timeout=6):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ('POST' if data else 'GET'),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def refresh_geo():
    with _lock:
        if time.time() - _cache['at'] < 1:
            return
        try:
            discover()
            ues = http_json(f"http://{_sbx['gis']}/gis/v1/geodata?assetType=UE").get('geoDataAssets', [])
            poas = http_json(f"http://{_sbx['gis']}/gis/v1/geodata?assetType=POA").get('geoDataAssets', [])
        except Exception:
            discover(force=True)
            ues = http_json(f"http://{_sbx['gis']}/gis/v1/geodata?assetType=UE").get('geoDataAssets', [])
            poas = http_json(f"http://{_sbx['gis']}/gis/v1/geodata?assetType=POA").get('geoDataAssets', [])
        _cache.update(at=time.time(), ues=ues, poas=poas)


def load_base_net():
    if _base_net:
        return
    # The active model already contains our netchar updates, so read the pristine values from the
    # scenario stored in platform-ctrl (never from /active, or degradations compound).
    name = http_json(f"http://{_sbx['ctrl']}/sandbox-ctrl/v1/active")['name']
    d = http_json(f"http://{platform_ctrl()}/platform-ctrl/v1/scenarios/{name}")
    for dom in d['deployment']['domains']:
        for z in dom.get('zones', []):
            for nl in z.get('networkLocations', []):
                if nl['type'].startswith('POA'):
                    nc = dict(nl.get('netChar') or {})
                    nc.setdefault('packetLoss', 0)
                    nc['latency'] = max(1, nc.get('latency', 1))  # latency 0 does not reset tc
                    _base_net[nl['name']] = {'type': nl['type'], 'netChar': nc}


def push_net(poa, nc):
    body = {'name': 'hazard-' + poa, 'type': 'NETWORK-CHARACTERISTICS-UPDATE',
            'eventNetworkCharacteristicsUpdate': {'elementName': poa, 'elementType': _base_net[poa]['type'], 'netChar': nc}}
    http_json(f"http://{_sbx['ctrl']}/sandbox-ctrl/v1/events/NETWORK-CHARACTERISTICS-UPDATE", body)


# ---------------------------------------------------------------- geometry
def dist_km(a, b):
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def move(p, heading_deg, km):
    lat = p[1] + km * math.cos(math.radians(heading_deg)) / 111.32
    lon = p[0] + km * math.sin(math.radians(heading_deg)) / (111.32 * math.cos(math.radians(p[1])))
    return [round(lon, 5), round(lat, 5)]


# ---------------------------------------------------------------- hazards
DEFAULTS = {
    'typhoon': {'radiusKm': 80, 'headingDeg': 30, 'speedKmh': 300, 'durationMin': 30},
    'tsunami': {'speedKmh': 500, 'maxRadiusKm': 250, 'quakeRadiusKm': 60, 'durationMin': 30},
}
# netChar degradation per impact level (added latency / packet loss % / throughput factor)
IMPACT = {
    'outer':   {'latency': 150, 'packetLoss': 10, 'tput': 0.3, 'label': 'Degraded'},
    'core':    {'latency': 400, 'packetLoss': 40, 'tput': 0.1, 'label': 'Severe'},
    'flooded': {'latency': 200, 'packetLoss': 25, 'tput': 0.2, 'label': 'Flooded'},
    'down':    {'latency': 1000, 'packetLoss': 100, 'tput': 0.01, 'label': 'Down'},
}
RANK = {None: 0, 'outer': 1, 'flooded': 2, 'core': 3, 'down': 4}


def make_hazard(body):
    kind = body.get('type')
    if kind not in DEFAULTS:
        raise ValueError('type must be typhoon or tsunami')
    c = body.get('center') or body.get('epicenter')
    if not (isinstance(c, list) and len(c) == 2 and all(isinstance(v, (int, float)) for v in c)):
        raise ValueError('center must be [lon, lat]')
    h = {**DEFAULTS[kind], **{k: body[k] for k in DEFAULTS[kind] if isinstance(body.get(k), (int, float))}}
    h.update(id=kind[:3] + '-' + secrets.token_hex(3), type=kind, name=str(body.get('name') or {'typhoon': 'Typhoon', 'tsunami': 'Tsunami'}[kind])[:40],
             origin=[float(c[0]), float(c[1])], createdAt=time.time(), affectNetwork=body.get('affectNetwork', True) is not False)
    return h


def geometry(h, now):
    el_h = (now - h['createdAt']) / 3600
    g = {k: h[k] for k in h}
    g['elapsedSec'] = round(now - h['createdAt'])
    g['remainingSec'] = max(0, round(h['durationMin'] * 60 - (now - h['createdAt'])))
    if h['type'] == 'typhoon':
        g['center'] = move(h['origin'], h['headingDeg'], h['speedKmh'] * el_h)
        g['coreRadiusKm'] = h['radiusKm'] * 0.4
        g['track'] = [move(g['center'], h['headingDeg'], h['speedKmh'] * m / 60) for m in (0, 5, 10, 15, 20)]
    else:
        g['center'] = h['origin']
        g['waveRadiusKm'] = round(min(h['maxRadiusKm'], h['speedKmh'] * el_h), 1)
    return g


def impact_at(g, p):
    d = dist_km(g['center'], p)
    if g['type'] == 'typhoon':
        return 'core' if d <= g['coreRadiusKm'] else 'outer' if d <= g['radiusKm'] else None
    if d <= g['quakeRadiusKm']:
        return 'down'
    return 'flooded' if d <= g['waveRadiusKm'] else None


def snapshot():
    """Hazards with geometry plus which POAs / ships they affect."""
    refresh_geo()
    now = time.time()
    with _lock:
        out = []
        for h in list(hazards.values()):
            g = geometry(h, now)
            g['poas'] = {p['assetName']: lvl for p in _cache['poas'] if (lvl := impact_at(g, p['location']['coordinates']))}
            g['ships'] = [u['assetName'] for u in _cache['ues'] if dist_km(g['center'], u['location']['coordinates']) <=
                          (g['radiusKm'] if g['type'] == 'typhoon' else g['maxRadiusKm'])]
            out.append(g)
        return out


def save():
    STATE.write_text(json.dumps(list(hazards.values()), ensure_ascii=False))


def net_loop():
    while True:
        try:
            snap = snapshot()
            with _lock:
                # expire
                for g in snap:
                    if g['remainingSec'] <= 0:
                        log(f"hazard expired {g['id']} {g['name']}")
                        hazards.pop(g['id'], None); save()
                snap = [g for g in snap if g['remainingSec'] > 0]
                load_base_net()
                worst = {}
                for g in snap:
                    if not g['affectNetwork']:
                        continue
                    for poa, lvl in g['poas'].items():
                        if RANK[lvl] > RANK[worst.get(poa)]:
                            worst[poa] = lvl
                for poa, base in _base_net.items():
                    b, lvl = base['netChar'], worst.get(poa)
                    if lvl:
                        i = IMPACT[lvl]
                        want = {'latency': b['latency'] + i['latency'], 'latencyVariation': max(b.get('latencyVariation', 0), i['latency'] // 4),
                                'packetLoss': max(b['packetLoss'], i['packetLoss']),
                                'throughputDl': max(1, int(b.get('throughputDl', 1000) * i['tput'])),
                                'throughputUl': max(1, int(b.get('throughputUl', 1000) * i['tput']))}
                    else:
                        want = None
                    if _pushed.get(poa) != (want or b):
                        push_net(poa, want or b)
                        _pushed[poa] = want or b
                        log(f"netchar {poa} -> {lvl or 'restore'} {want or b}")
                        if want:
                            _applied[poa] = {'level': lvl, 'netChar': want}
                        else:
                            _applied.pop(poa, None)
        except Exception as e:
            log(f'net_loop error: {e}')
        time.sleep(3)


def poa_status():
    return {poa: {'level': a['level'], 'label': IMPACT[a['level']]['label'], 'netChar': a['netChar'],
                  'base': _base_net.get(poa, {}).get('netChar')} for poa, a in _applied.items()}


# ---------------------------------------------------------------- http

# ---- route change history (who changed which ship's path, old -> new), shown on the map
_route_by = {}  # ship -> last 'by' of a route change (fleet-ai:avoid / :return / :restore ...)
ROUTE_LOG = HERE / 'route_changes.json'
ROUTE_LOG_KEEP = 300
try:
    _route_log = json.loads(ROUTE_LOG.read_text())[-ROUTE_LOG_KEEP:]
except Exception:
    _route_log = []
for _e in _route_log:  # survive restarts: last 'by' per ship from the saved history
    _route_by[_e['ship']] = _e['by']


def record_route_change(entry):
    with _lock:
        _route_log.append(entry)
        del _route_log[:-ROUTE_LOG_KEEP]
        try:
            ROUTE_LOG.write_text(json.dumps(_route_log))
        except OSError:
            pass


def route_changes(minutes=30.0):
    cut = time.time() - minutes * 60
    with _lock:
        return [e for e in _route_log if e['at'] >= cut]


# ---- route changes for other teams: POST /api/ships/{name}/route
EOP_MODES = ('LOOP', 'REVERSE', '')


def set_route(name, body):
    """Give a ship a new path. The current position is prepended so the ship turns where it is, never teleports."""
    pts = body.get('path')
    if isinstance(pts, dict):
        pts = pts.get('coordinates')
    if not isinstance(pts, list) or not 1 <= len(pts) <= 100:
        raise ValueError('path must be a list of 1-100 [lon, lat] points')
    clean = []
    for p in pts:
        if not (isinstance(p, (list, tuple)) and len(p) == 2 and all(isinstance(v, (int, float)) for v in p)):
            raise ValueError('each path point must be [lon, lat]')
        lon, lat = float(p[0]), float(p[1])
        if not (120 <= lon <= 140 and 28 <= lat <= 40):
            raise ValueError(f'point {p} is outside the Korea Strait area (expects [lon, lat])')
        clean.append([lon, lat])
    discover()
    try:
        cur = http_json(f"http://{_sbx['gis']}/gis/v1/geodata/{urllib.parse.quote(name)}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise LookupError(f'no ship named {name}')
        raise
    if not cur or cur.get('assetType') != 'UE':
        raise LookupError(f'no ship named {name}')
    here = cur['location']['coordinates']
    path = clean if body.get('fromCurrent') is False else [here] + clean
    if body.get('allowLand') is not True:
        bad = [p for p in clean if land.on_land(*p)]
        if bad:
            raise ValueError(f'waypoint on land: {bad[0]} (set allowLand: true to force)')
        hits = land.land_crossings(path)
        if hits:
            raise ValueError(f"path crosses land near {hits[0]['at']} on segment {hits[0]['segment']} (set allowLand: true to force)")
    mode = body.get('eopMode', cur.get('eopMode') or 'LOOP')
    if mode not in EOP_MODES:
        raise ValueError('eopMode must be LOOP, REVERSE or ""')
    vel = body.get('velocity', cur.get('velocity') or 250)
    if not isinstance(vel, (int, float)) or not 0 < vel <= 2000:
        raise ValueError('velocity must be a number in (0, 2000]')
    asset = {'assetName': name, 'assetType': 'UE', 'subType': cur.get('subType', 'UE'),
             'location': {'type': 'Point', 'coordinates': path[0]},
             'path': {'type': 'LineString', 'coordinates': path}, 'eopMode': mode, 'velocity': vel}
    http_json(f"http://{_sbx['gis']}/gis/v1/geodata/{urllib.parse.quote(name)}", asset)
    before = (cur.get('path') or {}).get('coordinates') or []
    reason = body.get('reason')
    record_route_change({'ship': name, 'at': time.time(), 'by': str(body.get('by') or 'unknown')[:40],
                         'reason': str(reason)[:160] if reason else None, 'before': before, 'after': path,
                         'from': here, 'eopMode': mode, 'velocity': vel})
    with _lock:
        _cache['at'] = 0  # show it on the next poll
    log(f'route changed {name} by {body.get("by", "?")}: {len(path)} points, {mode}, v={vel}')
    _route_by[name] = str(body.get('by', ''))
    return asset


class H(BaseHTTPRequestHandler):
    def send(self, code, obj, ctype='application/json'):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send(204, b'')

    def do_GET(self):
        path = self.path.split('?')[0]
        try:
            if path in ('/', '/index.html'):
                return self.send(200, HTML.read_bytes(), 'text/html; charset=utf-8')
            if path in ('/livemap.js', '/livemap/example.html'):
                f = HERE / 'livemap' / path.rsplit('/', 1)[1]
                return self.send(200, f.read_bytes(), 'text/javascript; charset=utf-8' if f.suffix == '.js' else 'text/html; charset=utf-8')
            if path == '/api/geodata':
                snap = snapshot()
                with _lock:
                    st = poa_status()
                    poas = [{**p, 'impact': st.get(p['assetName'])} for p in _cache['poas']]
                    return self.send(200, {'sandbox': _sbx['ns'], 'ts': time.time(), 'ues': [{**u, 'routeBy': _route_by.get(u.get('assetName'))} for u in _cache['ues']], 'poas': poas, 'hazards': snap, 'conflicts': conflicts(), 'routeChanges': route_changes(30)})
            if path == '/api/land':
                q = urllib.parse.parse_qs(self.path.partition('?')[2])
                if 'lon' in q and 'lat' in q:
                    return self.send(200, {'land': land.on_land(float(q['lon'][0]), float(q['lat'][0]))})
                return self.send(200, {'source': land._d['source'], 'bbox': land.BBOX, 'rings': land._d['rings']})
            if path == '/api/route-changes':
                q = urllib.parse.parse_qs(self.path.partition('?')[2])
                minutes = min(max(float(q.get('minutes', ['60'])[0]), 1), 24 * 60)
                return self.send(200, {'ts': time.time(), 'changes': route_changes(minutes)})
            if path == '/api/conflicts':
                return self.send(200, conflicts())
            if path == '/api/hazards':
                return self.send(200, {'ts': time.time(), 'hazards': snapshot()})
            parts = path.strip('/').split('/')
            if len(parts) == 3 and parts[0] == 'core' and parts[1] in CORE_PORTS and parts[2] == 'status':
                return self.send(200, core_status(parts[1]))
        except Exception as e:
            return self.send(503, {'error': str(e)})
        self.send(404, {'error': 'not found'})

    def do_POST(self):
        path = self.path.split('?')[0]
        parts = path.strip('/').split('/')
        if len(parts) == 4 and parts[:2] == ['api', 'ships'] and parts[3] == 'route':
            try:
                n = int(self.headers.get('Content-Length', 0))
                if not 0 < n <= 16384:
                    return self.send(413, {'error': 'invalid body size'})
                return self.send(200, set_route(urllib.parse.unquote(parts[2]), json.loads(self.rfile.read(n))))
            except LookupError as e:
                return self.send(404, {'error': str(e)})
            except (ValueError, json.JSONDecodeError) as e:
                return self.send(400, {'error': str(e)})
            except Exception as e:
                return self.send(502, {'error': 'GIS engine: ' + str(e)})
        if path != '/api/hazards':
            return self.send(404, {'error': 'not found'})
        try:
            n = int(self.headers.get('Content-Length', 0))
            if not 0 < n <= 8192:
                return self.send(413, {'error': 'invalid body size'})
            h = make_hazard(json.loads(self.rfile.read(n)))
        except (ValueError, json.JSONDecodeError) as e:
            return self.send(400, {'error': str(e)})
        with _lock:
            hazards[h['id']] = h; save()
        log(f"hazard created {h['id']} {h['name']} {h}")
        self.send(201, geometry(h, time.time()))

    def do_DELETE(self):
        path = self.path.split('?')[0]
        if not path.startswith('/api/hazards/'):
            return self.send(404, {'error': 'not found'})
        hid = path.rsplit('/', 1)[1]
        with _lock:
            gone = list(hazards) if hid == 'all' else [hid] if hid in hazards else []
            for k in gone:
                hazards.pop(k)
            save()
        log(f'hazard deleted {gone}')
        self.send(200 if gone else 404, {'deleted': gone})

    def log_message(self, *a):
        pass


if __name__ == '__main__':
    if STATE.exists():
        try:
            hazards.update({h['id']: h for h in json.loads(STATE.read_text())})
        except Exception:
            pass
    threading.Thread(target=net_loop, daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', PORT), H).serve_forever()
