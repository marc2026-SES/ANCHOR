import json
import os
from pathlib import Path
import runpy
import socket
import subprocess
import sys
import time
import threading
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
import requests

from anchor.client import Client, OneM2MError, canonical, resource
from anchor.config import Endpoint, Settings
from anchor.edge_forward import EdgeForwarder
from anchor.handover import edge_for_user, watch_location
from anchor.provision import bootstrap
from tests.test_tls_deployment import pki

ROOT = Path(__file__).resolve().parents[1]
ship_module = runpy.run_path(str(ROOT / 'hardware/ship-gateway-pi5/sync/anchor_relay.py'))


def observation(zone, poa, ship='ship-001'):
    return {'userList': {'user': [{'address': 'buoy-01', 'zoneId': 'jp-kyushu'},
                                 {'address': ship, 'zoneId': zone, 'accessPointId': poa}]}}


def test_location_keeps_route_on_satellite_and_missing_user():
    assert edge_for_user(observation('kr-busan', 'busan-ltem-1'), 'ship-001') == 'mep1'
    assert edge_for_user(observation('jp-kyushu', 'cell'), 'ship-001') == 'mep2'
    assert edge_for_user(observation('jp-kyushu', 'vsat-poa-2'), 'ship-001') is None
    assert edge_for_user(observation('vsat', 'vsat-poa-1'), 'ship-001') is None
    assert edge_for_user(observation('jp-kyushu', 'cell', 'ship-002'), 'ship-001') is None


def test_location_worker_switches_only_on_valid_coastal_observations(tmp_path):
    docs = [observation('kr-busan', 'cell'), observation('jp-kyushu', 'cell'),
            observation('vsat', 'vsat-poa-1'), None, observation('kr-busan', 'cell')]
    class Location(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == '/mep1/location/v3/queries/users'
            document = docs.pop(0)
            self.send_response(200 if document else 503)
            self.end_headers()
            self.wfile.write(json.dumps(document).encode())

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(('127.0.0.1', 0), Location) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = ship_module['Config'](ROOT / 'deploy/ship/relay.json')
        one = base.nodes.pop('shore')
        base.nodes.update(mep1=one, mep2=one)
        base.handover = {'location_url': f'http://127.0.0.1:{server.server_port}/mep1/location/v3/queries/users'}
        ship = ship_module['Relay'](base, tmp_path / 'queue.db')
        observations = []
        def tick(seconds):
            assert seconds == 2
            observations.append((ship.active, bool(ship.location_error)))
            if not docs:
                ship.stop.set()
        ship.stop.wait = tick
        try:
            watch_location(ship)
        finally:
            server.shutdown()
            ship.queue.db.close()
        assert observations == [('mep1', False), ('mep2', False), ('mep2', False),
                                ('mep2', True), ('mep1', False)]


def start_cse(stack, tmp, name, role, pki):
    path = tmp / name
    path.mkdir()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    ini = (ROOT / 'deploy/edge/cse.ini').read_text().replace('id-edge', 'id-' + name).replace('cse-edge', 'cse-' + name)
    ini = ini.replace('enableRemoteCSE=true', 'enableRemoteCSE=false').replace('httpPort=8080', f'httpPort={port}')
    ini = ini.replace('cseHost=edge.local', 'cseHost=127.0.0.1').replace('networkInterface=0.0.0.0', 'networkInterface=127.0.0.1')
    (path / 'acme.ini').write_text(ini)
    log = stack.enter_context((path / 'acme.log').open('w'))
    proc = subprocess.Popen([sys.executable, '-m', 'anchor.acme_tls', '--headless', '--base-directory', str(path),
                             '--config', 'acme.ini'], cwd=path, stdout=log, stderr=subprocess.STDOUT,
                            env=dict(os.environ, ANCHOR_TLS_DIR=str(pki/role)))
    def stop():
        proc.terminate()
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    stack.callback(stop)
    ep = Endpoint(f'https://127.0.0.1:{port}', f'cse-{name}', f'/id-{name}', ca=str(pki/role/'ca.crt'))
    client = Client(ep, 'CAnchor' + role.title())
    for _ in range(100):
        try:
            client.as_origin('CAdmin').get(ep.root)
            break
        except (requests.ConnectionError, OneM2MError):
            if proc.poll() is not None:
                pytest.fail((path/'acme.log').read_text()[-1700:])
            time.sleep(.1)
    bootstrap(Settings(role, {role: ep}), client)
    return ep


@pytest.mark.skipif(os.getenv('ANCHOR_TLS_WIRE_INTEGRATION') != '1', reason='local ACME HTTPS integration')
def test_handover_with_three_real_cses(tmp_path, pki, monkeypatch):
    with ExitStack() as stack:
        ship_ep = start_cse(stack, tmp_path, 'ship', 'ship', pki)
        a = start_cse(stack, tmp_path, 'edge', 'edge', pki)
        b = start_cse(stack, tmp_path, 'edge-b', 'edge', pki)
        shore = start_cse(stack, tmp_path, 'in', 'shore', pki)
        config = SimpleNamespace(nodes={'ship': ship_ep, 'mep1': a, 'mep2': b}, origin='CAnchorShip',
                                 handover={}, vessel_id='ship-001')
        db = tmp_path / 'ship.db'
        ship = ship_module['Relay'](config, db)
        def edge(ep):
            cfg = SimpleNamespace(nodes={'edge': ep, 'shore': shore}, origin='CAnchorEdge')
            return EdgeForwarder(cfg, tmp_path / (ep.root + '.db'))
        workers = [edge(a), edge(b)]
        def pass_queue():
            ship.stop.clear()
            with monkeypatch.context() as m:
                m.setattr(ship.stop, 'wait', lambda _: ship.stop.set())
                m.setattr(ship.wake, 'wait', lambda _: ship.stop.set())
                ship.forward_loop()
        def queue(i):
            event_id = f'ship-001:engine:boot:{i}'
            rn = ship_module['record_name'](event_id)
            body = canonical({'eventId': event_id, 'seq': i})
            ship.queue.put(rn, body)
            return rn, body
        assert ship.active is None
        ship.select_edge('mep1', time.time())
        first = queue(1)
        pass_queue()  # A has custody. Ship no longer holds it.
        assert ship.queue.size() == 0 and ship.status()['delivery']['edgeStored'] == 1
        second = queue(2)
        create = ship.targets['mep1'].client.create
        def lost_ack(*args):
            create(*args)
            raise requests.Timeout('lost reply after A stored the event')
        with monkeypatch.context() as m:
            m.setattr(ship.targets['mep1'].client, 'create', lost_ack)
            pass_queue()
        assert ship.queue.size() == 1
        ship.select_edge('mep2', time.time())
        ship.queue.db.close()
        ship = ship_module['Relay'](config, db)
        assert ship.active == 'mep2'
        third = queue(3)
        pass_queue()
        assert ship.queue.size() == 0
        # No callbacks were delivered: recovery from the retained edge CINs is sufficient.
        for w in workers:
            while w.advance():
                pass
        for ep, expected in [(a, [first, second]), (b, [second, third]), (shore, [first, second, third])]:
            client = Client(ep, 'CAnchorEdge')
            path = ep.root + '/anchor/events'
            assert resource(client.get(path))['cni'] == len(expected)
            for rn, body in expected:
                assert resource(client.get(path + '/' + rn))['con'] == body
        for w in workers:
            w.db.close()
        for ep in (a, b):
            restarted = edge(ep)
            assert not restarted.advance()
            restarted.db.close()
        ship.select_edge('mep1', time.time())
        fourth = queue(4)
        pass_queue()
        w = edge(a)
        assert w.advance()
        assert resource(w.target.client.get(w.target.parent + '/' + fourth[0]))['con'] == fourth[1]
        w.db.close()

        # Exercise actual TLS SUB notifications at the ship and the selected edge.
        def subscribe(worker, role):
            cfg = worker.config
            cfg.role, cfg.bind, cfg.port = role, '127.0.0.1', 0
            cfg.tls_cert, cfg.tls_key = str(pki/role/'server.crt'), str(pki/role/'server.key')
            svc = ship_module['make_server'](worker, set())
            cfg.notify_url = f'https://127.0.0.1:{svc.server_port}/notify'
            threading.Thread(target=svc.serve_forever, daemon=True).start()
            stack.callback(svc.server_close)
            stack.callback(svc.shutdown)
            worker.discover()

        w = edge(b)
        subscribe(w, 'edge')
        subscribe(ship, 'ship')
        ship.select_edge('mep2', time.time())
        sensor = Client(ship_ep, 'CEngine')
        event = {'seq': 9, 'bootId': 'wire-test', 'vib_rms_g': 0.01}
        sensor.create(ship_ep.root + '/engine/vibration', 4, {'m2m:cin': {'con': canonical(event)}})
        for _ in range(100):
            if ship.queue.size():
                break
            time.sleep(.03)
        [(row_id, rn, envelope)] = ship.queue.head(1)
        assert rn == ship_module['record_name']('ship-001:engine:wire-test:9')
        pass_queue()
        for _ in range(100):
            if w.hints:
                break
            time.sleep(.03)
        assert rn in w.hints.values()
        assert w.advance()
        assert resource(w.target.client.get(w.target.parent + '/' + rn))['con'] == envelope
        w.db.close()
        ship.queue.db.close()


@pytest.mark.parametrize('filename', ['shore_monitor.py', 'shore_monitor_pi.py'])
def test_dashboard_distinguishes_ship_backlog_from_unobserved_shore_receipts(filename):
    monitor = runpy.run_path(str(ROOT / 'dashboard/server' / filename))['Monitor']
    state = SimpleNamespace(core_status={'ship': {'at': 1000, 'delivery': {
        'accepted': 4, 'edgeStored': 3, 'pending': 1}}}, cores={'ship': SimpleNamespace(ok=True)})
    assert monitor.backlog(state) == {'generated': 4, 'heldOnShip': 1, 'shipAt': 1000}
