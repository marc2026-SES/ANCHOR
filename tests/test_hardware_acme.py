"""Three real isolated ACME processes. No real ESP32 or radio is implied.

ANCHOR_HARDWARE_INTEGRATION=1 pytest tests/test_hardware_acme.py -v
"""
import copy
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from anchor.client import Client, OneM2MError, resource
from anchor.commands import dispatch, make
from anchor.config import Endpoint, Settings
from anchor.events import digest, record_name
from anchor.runtime import Runtime
from anchor.service import make_server
from anchor.store import CapacityError, Journal

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get('ANCHOR_HARDWARE_INTEGRATION') != '1', reason='opt-in three-CSE hardware integration')]


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@pytest.fixture(scope='module')
def fleet(tmp_path_factory):
    base = tmp_path_factory.mktemp('hardware-acme')
    nodes, processes, runtimes, servers = {}, {}, {}, []
    configs = {}
    def start(role):
        directory = base / role
        with (directory / 'process.log').open('a') as log:
            process = subprocess.Popen([sys.executable, '-m', 'acmecse', '--headless',
                                        '--base-directory', str(directory), '--config', 'acme.ini'],
                                       cwd=directory, stdout=log, stderr=subprocess.STDOUT)
        processes[role] = process
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                Client(nodes[role], 'CAdmin').get(nodes[role].root)
                return
            except Exception:
                if process.poll() is not None:
                    break
                time.sleep(.1)
        pytest.fail((directory / 'process.log').read_text()[-4000:])
    try:
        for role, root, csi in [('ship', 'cse-ship', '/id-ship'), ('edge', 'cse-edge', '/id-edge'), ('shore', 'cse-in', '/id-in')]:
            directory = base / role; directory.mkdir()
            p = port()
            nodes[role] = Endpoint(f'http://127.0.0.1:{p}', root, csi, timeout=2)
            (directory / 'acme.ini').write_text(f'''[basic.config]
cseType={'IN' if role == 'shore' else 'MN'}
cseID={csi[1:]}
cseName={root}
cseHost=127.0.0.1
httpPort={p}
databaseType=tinydb
adminID=CAdmin
networkInterface=127.0.0.1
serviceProviderID=//anchor.test
[cse]
enableRemoteCSE=false
requestExpirationDelta=2
[resource.cnt]
enableLimits=false
[database.tinydb]
cacheSize=0
writeDelay=0
[mqtt]
enable=false
[logging]
level=warn
enableScreenLogging=false
[textui]
enable=false
[webui]
enable=false
''')
            start(role)
        for role in nodes:
            cfg = Settings(role, nodes, port=port())
            cfg.notify_url = f'http://127.0.0.1:{cfg.port}/notify'
            configs[role] = cfg
            runtime = Runtime(cfg)
            server = make_server(runtime, 'test-token-with-at-least-24-characters')
            threading.Thread(target=server.serve_forever, daemon=True).start()
            servers.append(server)
            runtime.provision()
            runtimes[role] = runtime
        yield runtimes, nodes, processes, start, configs
    finally:
        for server in servers:
            server.shutdown(); server.server_close()
        for process in processes.values():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(10)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()


def emit(fleet, seq=0, boot='demo', roll=30, sensor='attitude', device='bridge', **fields):
    runtimes, nodes, *_ = fleet
    payload = {'bootId': boot, 'seq': seq, 'ts': (1794787200 + seq) * 1000,
               'deviceId': device, 'vesselId': 'ship-001', 'roll': roll, 'pitch': 0, **fields}
    client = Client(nodes['ship'], 'C' + device.title())
    client.put_record(client.root + '/' + device + '/' + sensor, record_name(boot + str(seq)), payload)
    return payload


def test_provision_is_repeatable_and_hardware_acp_is_enforced(fleet):
    runtimes, nodes, *_ = fleet
    for runtime in runtimes.values():
        runtime.provision()
    bridge = Client(nodes['ship'], 'CBridge')
    bridge.get('cse-ship/bridge/attitude')
    with pytest.raises(OneM2MError) as denied:
        bridge.get('cse-ship/cargo/climate')
    assert denied.value.rsc == 4103
    dashboard = Client(nodes['ship'], 'CAnchorDashboard')
    with pytest.raises(OneM2MError):
        dashboard.get('cse-ship/bridge/position')
    assert resource(bridge.get('cse-ship/bridge/attitude')).get('mni') is None


def test_nonoverlapping_contact_windows_restart_and_final_receipt(fleet):
    runtimes, nodes, processes, start, configs = fleet
    ship, edge, shore = (runtimes[r] for r in ('ship', 'edge', 'shore'))
    # Backhaul really unavailable: shore CSE process is stopped.
    processes['shore'].terminate(); processes['shore'].wait(10)
    for n in range(3):
        emit(fleet, n)
    ship.local_tick()
    assert len(ship.status()['incidents']) == 1
    batch = ship.capture_batch('nonoverlap')
    ship.transfer_tick('edge'); edge.local_tick(); ship.transfer_tick('edge'); ship.local_tick()
    assert ship.status()['delivery']['coastCustody'] == len(batch['expected'])
    assert ship.status()['delivery']['shoreConfirmed'] == 0
    assert not ship.batch_status('nonoverlap')['complete']
    # Ship leaves; restart coastal CSE + application to eliminate memory authority.
    processes['ship'].terminate(); processes['ship'].wait(10)
    processes['edge'].terminate(); processes['edge'].wait(10)
    start('edge'); start('shore')
    edge = runtimes['edge'] = Runtime(configs['edge']); edge.ready = True
    edge.local_tick(); edge.transfer_tick('shore'); shore.local_tick(); edge.transfer_tick('shore')
    assert shore.status()['delivery']['shoreConfirmed'] == len(batch['expected'])
    assert processes['ship'].poll() is not None
    # Next ship contact returns final receipts; ship originals are still present.
    start('ship')
    ship = runtimes['ship'] = Runtime(configs['ship']); ship.ready = True
    ship.local_tick(); ship.transfer_tick('edge'); ship.local_tick()
    assert ship.batch_status('nonoverlap')['complete']
    assert ship.status()['delivery']['accepted'] == len(batch['expected'])


def test_lost_ack_duplicate_and_hash_conflict_do_not_inflate_delivery(fleet):
    runtimes, *_ = fleet
    ship, edge = runtimes['ship'], runtimes['edge']
    event = ship._events[0]
    before = len(edge._events)
    assert not Journal(ship.clients['edge'], ship.settings).put('events', event['eventId'], event)
    corrupt = copy.deepcopy(event); corrupt['payload']['roll'] = 99
    with pytest.raises(ValueError, match='Conflicting'):
        Journal(ship.clients['edge'], ship.settings).put('events', event['eventId'], corrupt)
    edge.local_tick()
    assert len(edge._events) == before


def test_bad_cin_is_quarantined_without_stopping_other_sources(fleet):
    runtimes, nodes, *_ = fleet
    c = Client(nodes['ship'], 'CBridge')
    c.create('cse-ship/bridge/attitude', 4, {'m2m:cin': {'con': '{not json'}})
    emit(fleet, 0, boot='healthy', roll=0)
    ship = runtimes['ship']; ship.local_tick()
    assert any(e['bootId'] == 'healthy' for e in ship._events)
    assert list(ship.journal.values('quarantine'))


def test_cached_command_group_dispatch_and_real_result_boundary(fleet):
    runtimes, nodes, processes, start, _ = fleet
    ship, edge, shore = (runtimes[r] for r in ('ship', 'edge', 'shore'))
    command = shore.submit_command({'commandId': 'profile-demo', 'ttlMs': 120000})
    assert shore.submit_command({'commandId': 'profile-demo', 'ttlMs': 120000}) == command
    edge.cache_commands_tick('shore'); edge.local_tick()
    processes['shore'].terminate(); processes['shore'].wait(10)
    try:
        ship.cache_commands_tick('edge'); ship.local_tick()
        status = dispatch(ship.local, ship.journal, command, int(time.time()*1000), {})
        assert status == 'dispatched'
        for device in ('bridge', 'engine'):
            assert json.loads(resource(ship.local.get('cse-ship/' + device + '/command/' + record_name(command['commandId'])))['con']) == command
        assert not any(e['kind'] == 'commandResult' for e in ship._events)
        # Test fixture emulates ONLY the device result CIN. No real ESP32 is claimed.
        for device in ('bridge', 'engine'):
            emit(fleet, 0, boot='result-' + device, device=device, sensor='commandResults',
                 commandId=command['commandId'], status='applied', driver='esp32', appliedIntervalMs=200,
                 command=command, appliedAtMs=command['issuedAtMs'],
                 profileUntilMs=command['issuedAtMs'] + command['durationMs'])
        ship.local_tick()
        assert next(c for c in ship.status()['commands'] if c['commandId'] == command['commandId'])['status'] == 'applied'
        expired = make({'commandId': 'expired'}, now=1)
        assert dispatch(ship.local, ship.journal, expired, 100000, {}) == 'expired'
        with pytest.raises(OneM2MError) as missing:
            ship.local.get('cse-ship/bridge/command/' + record_name('expired'))
        assert missing.value.rsc == 4004
    finally:
        start('shore')


def test_backpressure_keeps_previously_accepted_records(fleet):
    runtime = fleet[0]['edge']
    settings = copy.copy(runtime.settings)
    settings.max_records = 1
    journal = Journal(runtime.local, settings)
    before = list(journal.values('events'))
    with pytest.raises(CapacityError):
        journal.put('events', 'extra', {'not': 'accepted'})
    assert list(journal.values('events')) == before


def test_edge_pch_is_independent_of_shore(fleet):
    runtimes, _, processes, start, _ = fleet
    ship, edge = runtimes['ship'], runtimes['edge']
    processes['shore'].terminate(); processes['shore'].wait(10)
    errors = []
    def hint():
        try:
            edge.hint_tick()
        except Exception as exc:
            errors.append(exc)
    try:
        t = threading.Thread(target=hint); t.start()
        for _ in range(3):
            ship.poll_tick('edge')
            if ship.pch_received:
                break
        t.join(5)
        assert not errors
        assert ship.pch_received >= 1
    finally:
        start('shore')
