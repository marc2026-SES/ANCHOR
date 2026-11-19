"""Build the two-edge base scenario 'anchor-edge-mec' = team scenario 'anchor-korea-strait' + ANCHOR ship/coast processes.

usage: python3 make_base_scenario.py anchor-korea-strait.json anchor-edge-mec.json   (run make_certs.sh once first)
Then make_scenario.py turns it into the relay handover scenario:
  python3 make_scenario.py anchor-edge-mec.json anchor-core-handover.json --image <relay image> \
      --mirror-command /usr/local/bin/anchor-mirror
The three app processes start as the mirror image here; make_scenario.py swaps them to the relay image and runs
the mirror image as a separate process on ship-001.
  ship    : new UE 'ship-001'  -> anchor-ship-cse (mirror of the real ship CSE) + anchor-ship-app
            The ship core hands over between the two coastal CSEs by MEC 013 zone (kr-busan -> A, jp-kyushu -> B).
  coast A : MEC host mep1      -> anchor-edge-cse   (cse-edge,   :30080) + anchor-edge-app   (:30790)
  coast B : MEC host mep2      -> anchor-edge-b-cse (cse-edge-b, :30084) + anchor-edge-b-app (:30794)
  shore   : the REAL shore Pi (REAL_SHORE, verified with the Pi CA). No shore process runs in the sandbox.
All sandbox links are HTTPS, verified with the sandbox CA (SANDBOX_TLS, outside the repo).
"""
import base64, copy, json, os, sys, uuid

NODE_IP = '100.96.155.4'
REAL_SHIP = 'https://100.114.83.111:8443'  # ANCHOR CSE on the ship Pi (8080 is the legacy hardware CSE)
REAL_SHORE = 'https://100.98.242.10:8443'
# CA that signed the real Pi certificates (pi-ca.crt from the team). Padding is stripped: scenario env is K=V,K=V.
SHORE_CA = os.environ.get('SHORE_CA_FILE', os.path.expanduser('~/anchor-secrets/secrets/pi-ca.crt'))
TAG = os.environ.get('IMAGE_TAG', 'latest')
ACME, APP = f'meep-docker-registry:30001/anchor-acme:{TAG}', f'meep-docker-registry:30001/anchor-mirror:{TAG}'
SANDBOX_TLS = os.environ.get('SANDBOX_TLS', os.path.expanduser('~/anchor-secrets/sandbox-tls'))
LOCATION_URL = 'http://mep1-meep-loc-serv/location/v3'  # MEC 013 inside the sandbox (all zones)


def env(**kw):
    return ','.join(f'{k}={v}' for k, v in kw.items())


def proc(tpl, name, ptype, image, environment, port, ext, deps=()):
    p = copy.deepcopy(tpl)
    p.update(id=str(uuid.uuid4()), name=name, type=ptype, image=image, environment=environment,
             dependencies=list(deps), serviceConfig={'name': name, 'ports': [{'protocol': 'TCP', 'port': port, 'externalPort': ext}]})
    return p


base, out = sys.argv[1:3]
# Fixed dashboard tokens (deploy/mec/tokens.json, gitignored) so redeploys keep the same token
_tok = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tokens.json')
TOKENS = json.load(open(_tok)) if os.path.exists(_tok) else {}


def tok(role):
    return {'ANCHOR_API_TOKEN': TOKENS[role]} if role in TOKENS else {}

d = json.load(open(base))
d['name'] = 'anchor-edge-mec'
d.pop('id', None)
pls = {}
for dom in d['deployment']['domains']:
    for z in dom.get('zones', []):
        for nl in z.get('networkLocations', []):
            for pl in nl.get('physicalLocations', []):
                pls[pl['name']] = (nl, pl)
tpl = next(p for p in pls['mep1'][1]['processes'] if p['name'] == 'anchor-fleet')

def b64(path):
    return base64.b64encode(open(path, 'rb').read()).decode().rstrip('=')  # env is K=V,K=V: no '=' padding


pi_ca = b64(SHORE_CA)
TLS = {'TLS_CA_B64': b64(f'{SANDBOX_TLS}/ca.crt'), 'TLS_CERT_B64': b64(f'{SANDBOX_TLS}/server.crt'),
       'TLS_KEY_B64': b64(f'{SANDBOX_TLS}/server.key')}


def coast(suffix, cse_id, cse_name, cse_port, app_port):
    cse, app = f'anchor-edge{suffix}-cse', f'anchor-edge{suffix}-app'
    return [proc(tpl, cse, 'EDGE-APP', ACME, env(ROLE='edge', CSE_ID=cse_id, CSE_NAME=cse_name, PUBLIC_HOST=NODE_IP,
                                                 HTTP_PORT=cse_port, **TLS), cse_port, cse_port),
            proc(tpl, app, 'EDGE-APP', APP, env(ROLE='edge', EDGE_CSE_URL=f'https://{cse}:{cse_port}', EDGE_CSE_ROOT=cse_name,
                                                EDGE_CSE_ID='/' + cse_id, SHORE_CSE_URL=REAL_SHORE, SHORE_CA_B64=pi_ca,
                                                SHORE_TIMEOUT=30, APP_SVC=app, **TLS, **tok('edge')), 8790, app_port, [cse])]


edge = coast('', 'id-edge', 'cse-edge', 30080, 30790)
edge_b = coast('-b', 'id-edge-b', 'cse-edge-b', 30084, 30794)
ship = [proc(tpl, 'anchor-ship-cse', 'UE-APP', ACME, env(ROLE='ship', PUBLIC_HOST='anchor-ship-cse', HTTP_PORT=8080, **TLS), 8080, 30082),
        proc(tpl, 'anchor-ship-app', 'UE-APP', APP, env(ROLE='ship', SHIP_CSE_URL='https://anchor-ship-cse:8080',
                                                         EDGE_CSE_URL='https://anchor-edge-cse:30080',
                                                         EDGE_B_CSE_URL='https://anchor-edge-b-cse:30084', LOCATION_URL=LOCATION_URL,
                                                         APP_SVC='anchor-ship-app', SOURCE_SHIP_URL=REAL_SHIP, SOURCE_CA_B64=pi_ca,
                                                         SINCE_MINUTES=10, **TLS, **tok('ship')), 8790, 30792, ['anchor-ship-cse'])]

pls['mep1'][1]['processes'] += edge
pls['mep2'][1]['processes'] += edge_b
nl, ue_tpl = pls['ferry-hakata']
ue = copy.deepcopy(ue_tpl)
ue.update(id=str(uuid.uuid4()), name='ship-001', processes=ship, macId='0a0c510000a1')
# Position is simulated (the real Pi is not at sea; its GPS is unused). Loop through every cell type:
# Sea only: Busan -> east channel past Tsushima -> open sea (VSAT) -> off Fukuoka -> north of Iki -> south of Tsushima -> west channel -> Busan
ROUTE = [[129.15, 35.0], [129.6, 34.7], [130.0, 34.4], [130.3, 33.75], [129.85, 33.9], [129.45, 34.0], [129.1, 34.0], [128.95, 34.3], [129.0, 34.7], [129.15, 35.0]]  # checked with ~/anchor-map/land.py
ue['geoData'] = {**ue_tpl.get('geoData', {}), 'location': {'type': 'Point', 'coordinates': ROUTE[0]},
                 'path': {'type': 'LineString', 'coordinates': ROUTE}, 'eopMode': 'LOOP', 'velocity': 250}
nl['physicalLocations'].append(ue)
# The web UI trims UEs to these default counts (alphabetically) right after deploy.
# ship-001 moves at 250 (>= highVelocitySpeedThreshold), so raise the high-velocity count or the UI removes it.
meta = d['deployment'].setdefault('userMeta', {})
ue_meta = json.loads(meta.get('mec-sandbox', '{}'))
ue_meta['defaultHighVelocityUeCount'] = int(ue_meta.get('defaultHighVelocityUeCount', 0)) + 1
meta['mec-sandbox'] = json.dumps(ue_meta)
json.dump(d, open(out, 'w'))
print('ok', out)
