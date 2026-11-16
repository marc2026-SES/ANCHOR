"""Opt-in two-role process test on one host, using separate DBs/certs/services.

Stop the dev profile first. This uses direct loopback peer endpoints in place of
the SSH tunnel; SSH and Pi hardware are not claimed by this test.
"""
import json
import os
import subprocess
import sys
import time

import pytest
import requests

from anchor.lab.config import PROJECT

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get('ANCHOR_SPLIT_INTEGRATION') != '1', reason='opt-in split-role test')]


@pytest.fixture(scope='module')
def split(tmp_path_factory):
    topology = tmp_path_factory.mktemp('topology') / 'ship.json'
    topology.write_text(json.dumps({'nodes': {'shore': {'port': 8700}, 'edge': {'port': 8701}}}))

    def run(role, *args, script=None, check=True):
        env = {k: v for k, v in os.environ.items() if k not in ('ANCHOR_ROLE', 'ANCHOR_RUNTIME', 'ANCHOR_TOPOLOGY', 'ANCHOR_CA')}
        env['ANCHOR_ROLE'] = role
        if role == 'ship':
            env['ANCHOR_TOPOLOGY'] = str(topology)
        cmd = [sys.executable, '-c', script] if script else [sys.executable, '-m', 'anchor.lab.entrypoint', '--role', role, *args]
        p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120, cwd=PROJECT)
        if check and p.returncode:
            pytest.fail(f"Role {role}, command {args} failed: {p.stderr[-2000:]}")
        return json.loads(p.stdout) if check else p

    try:
        run('shore', 'up')
        assert set(run('shore', 'bootstrap')) == {'shore', 'edge'}
        # Shore provisioning and queue submission work before ship startup.
        queued = run('shore', 'command', 'safetyGroup', 'on', '--ttl', '600')
        run('ship', 'prepare')
        run('ship', 'trust-shore', str(PROJECT / '.runtime-shore/certs/localhost.crt'))
        run('ship', 'up')
        assert set(run('ship', 'bootstrap')) == {'ship'}
        run('ship', 'scenario', 'coastal')
        wait_for(lambda: run('ship', 'status')['state']['phase'] == 'ACTIVE')
        yield run, queued
    finally:
        run('ship', 'down', check=False)
        run('shore', 'down', check=False)


def wait_for(check, seconds=45):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.3)
    raise AssertionError('Split-role condition timed out')


def test_only_owned_processes_and_role_command_boundaries(split):
    run, _ = split
    assert set(run('ship', 'info')['processes']) == {'database', 'broker', 'ship', 'service'}
    assert set(run('shore', 'info')['processes']) == {'database', 'shore', 'edge', 'service'}
    assert run('shore', 'emit', check=False).returncode == 2
    assert run('ship', 'command', 'fan', 'on', check=False).returncode == 2
    assert run('ship', 'restart', 'shore', check=False).returncode == 2


def test_shore_queued_command_and_mqtt_delivery(split):
    run, queued = split
    def results():
        return run('shore', script="from anchor.lab.client import Client; import json; print(json.dumps([v for _,v in Client('shore').records('shore/anchor/commandResults')]))")
    wait_for(lambda: any(v['commandId'] == queued['commandId'] and v['status'] == 'executed' for v in results()))
    events = run('ship', 'emit', '--count', '5')['events']
    ids = {e['eventId'] for e in events}
    def received():
        return set(run('shore', script="from anchor.lab.client import Client; import json; print(json.dumps([v['eventId'] for _,v in Client('shore').records('shore/anchor/events')]))"))
    wait_for(lambda: ids.issubset(received()))
    before = run('ship', 'status')['pollingChannelReceived']
    run('shore', 'command', 'fan', 'off')
    wait_for(lambda: run('ship', 'status')['pollingChannelReceived'] > before)


def test_shore_service_is_independent_of_ship_service(split):
    run, _ = split
    run('ship', script="from anchor.lab.stack import stop_node; import json; stop_node('service'); print(json.dumps({'stopped': True}))")
    try:
        assert run('shore', 'status')['state']['phase'] == 'READY'
        queued = run('shore', 'command', 'buzzer', 'off', '--ttl', '120')
    finally:
        run('ship', 'restart', 'service')
    def completed():
        values = run('shore', script="from anchor.lab.client import Client; import json; print(json.dumps([v for _,v in Client('shore').records('shore/anchor/commandResults')]))")
        return any(v['commandId'] == queued['commandId'] and v['status'] == 'executed' for v in values)
    wait_for(completed)
