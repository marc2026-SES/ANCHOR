"""Translate the existing ANCHOR AdvantEDGE process environment into core relay config."""
import base64
import json
import os
from pathlib import Path
import sys
import tempfile

from anchor.client import Client
from anchor.config import Endpoint, ORIGINS, Settings
from anchor.provision import bootstrap


def build_config(env, runtime):
    def pem(key, filename):
        value = env[key].strip()
        path = runtime / filename
        path.write_bytes(base64.b64decode(value + '=' * (-len(value) % 4), validate=True))
        return str(path)

    role = env['ROLE']
    ca = pem('TLS_CA_B64', 'ca.crt')
    cert = pem('TLS_CERT_B64', 'server.crt')
    key = pem('TLS_KEY_B64', 'server.key')
    state = Path(env.get('ANCHOR_STATE_DIR', '/var/lib/anchor'))
    config = dict(role=role, bind='0.0.0.0', port=8790, tls_cert=cert, tls_key=key,
                  notify_url=f"https://{env['APP_SVC']}:8790/notify",
                  queue=str(state / 'relay.sqlite'), nodes={})
    if env.get('ANCHOR_API_TOKEN'):
        token = runtime / 'status.token'
        token.write_text(env['ANCHOR_API_TOKEN'])
        config['status_token_files'] = [str(token)]

    def node(name, url, root, csi, verify=ca):
        config['nodes'][name] = dict(url=url, root=root, csi=csi, ca=verify)

    if role == 'ship':
        node('ship', env['SHIP_CSE_URL'], 'cse-ship', '/id-ship')
        node('mep1', env['EDGE_CSE_URL'], 'cse-edge', '/id-edge')
        node('mep2', env['EDGE_B_CSE_URL'], 'cse-edge-b', '/id-edge-b')
        config['handover'] = dict(location_url=env['LOCATION_URL'].rstrip('/') + '/queries/users',
                                  address=env.get('LOCATION_ADDRESS', 'ship-001'), ca=ca)
    elif role == 'edge':
        node('edge', env['EDGE_CSE_URL'], env['EDGE_CSE_ROOT'], env['EDGE_CSE_ID'])
        shore_ca = pem('SHORE_CA_B64', 'shore-ca.crt') if env.get('SHORE_CA_B64') else ca
        node('shore', env['SHORE_CSE_URL'], 'cse-in', '/id-in', shore_ca)
    else:
        raise ValueError('Expected ship or edge role')
    return config


def main():
    os.umask(0o077)
    runtime = Path(tempfile.mkdtemp(prefix='anchor-relay-'))
    config = build_config(os.environ, runtime)
    path = runtime / 'relay.json'
    path.write_text(json.dumps(config))
    # Install only this process's local CSE resources, using the core branch ACPs.
    role = config['role']
    endpoint = Endpoint(**config['nodes'][role])
    bootstrap(Settings(role, {role: endpoint}, dashboard_edge_read=True), Client(endpoint, ORIGINS[role]))
    script = Path(__file__).resolve().parents[2] / 'hardware/ship-gateway-pi5/sync/anchor_relay.py'
    os.execv(sys.executable, [sys.executable, str(script), '--config', str(path)])


if __name__ == '__main__':
    main()
