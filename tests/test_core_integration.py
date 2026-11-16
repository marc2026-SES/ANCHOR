"""Opt-in acceptance checks against unmodified running ACME, not mocks.

Run ANCHOR_INTEGRATION=1 pytest tests/test_core_integration.py -v.
Uses the local development stack; creates uniquely named telemetry/commands and
restarts owned service/CSE processes. Do not run against a shared competition CSE.
"""
import json
import os
import subprocess
import sys
import time
import uuid

import pytest
import requests

from anchor.lab.cli import api, emit
from anchor.lab.client import Client, OneM2MError, resource
from anchor.lab.config import ACME, RUNTIME
from anchor.lab.models import record_name

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get("ANCHOR_INTEGRATION") != "1", reason="opt-in local ACME acceptance test")]


def eventually(check, timeout=35):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        try:
            last = check()
            if last:
                return last
        except (OneM2MError, requests.ConnectionError) as exc:
            last = str(exc)
        time.sleep(0.25)
    raise AssertionError(f"Condition not met within {timeout}s: {last}; status={api('/status')}")


def route(zone):
    api("/scenario", {"zone": zone})
    target = None if zone == "shadow" else zone
    phase = "DISCONNECTED" if target is None else "ACTIVE"
    return eventually(lambda: (s if (s := api('/status'))['state']['route'] == target
                              and s['state']['phase'] == phase else None))


def result(command):
    return json.loads(resource(Client('shore').get(
        'shore/anchor/commandResults/' + record_name(command['commandId'])))["con"])


def assert_delivered(events):
    def check():
        received = {e['eventId']: e for _, e in Client('shore').records('shore/anchor/events')}
        return all(received.get(e['eventId']) == e for e in events)
    eventually(check)


@pytest.fixture(scope="module", autouse=True)
def bootstrap():
    first = api('/bootstrap', {})
    assert api('/bootstrap', {}) == first
    yield
    route('coastal')


def test_upstream_is_clean_and_no_adapters_are_loaded():
    assert subprocess.check_output(['git', '-C', str(ACME), 'status', '--porcelain'], text=True) == ''
    for node in ('ship', 'shore', 'edge'):
        assert not list((RUNTIME / node / 'plugins').glob('Anchor*.py'))
    src = (RUNTIME.parent / 'src' / 'anchor')
    assert not any('from acmecse' in p.read_text() for p in src.glob('*.py'))


def test_mqtt_transit_notifications_and_idempotent_delivery():
    route('coastal')
    before = api('/status')['notificationRequests']
    events = emit(4, 'mqtt')
    assert_delivered(events)
    eventually(lambda: api('/status')['notificationRequests'] > before)
    e = events[0]
    assert not Client('shore').put_record('shore/anchor/events', record_name(e['eventId']), e)
    with pytest.raises(ValueError, match='Conflicting immutable'):
        Client('shore').put_record('shore/anchor/events', record_name(e['eventId']), {**e, 'priority': 'critical'})


def test_acp_denies_cross_device_and_dashboard_mutation():
    sensor = Client('ship', 'Csensor01')
    with pytest.raises(OneM2MError) as denied:
        sensor.get('ship/sensor-02/history')
    assert denied.value.rsc == 4103
    for node in ('shore', 'edge'):
        dashboard = Client(node, 'CAnchorDashboard')
        dashboard.get('/id-shore/shore/anchor/events')
        with pytest.raises(OneM2MError) as denied:
            dashboard.get('/id-shore/shore/anchor/location')
        assert denied.value.rsc == 4103
        with pytest.raises(OneM2MError) as denied:
            dashboard.put_record('/id-shore/shore/anchor/commands', 'forbidden', {'bad': True})
        assert denied.value.rsc == 4103


def test_polling_channel_and_group_fanout():
    route('coastal')
    before = api('/status')['pollingChannelReceived']
    request = {'target': 'safetyGroup', 'desiredState': True, 'ttl': 60, 'commandId': uuid.uuid4().hex}
    command = api('/commands', request)
    assert api('/commands', request) == command
    eventually(lambda: result(command)['status'] == 'executed')
    eventually(lambda: api('/status')['pollingChannelReceived'] > before)
    for device in ('fan', 'buzzer'):
        assert resource(Client('ship').get(f'ship/{device}/reportedState'))['state'] is True
        commands = Client('ship').records(f'ship/{device}/commands')
        assert sum(c['commandId'] == command['commandId'] for _, c in commands) == 1


def test_outage_restart_replay_expiration_and_batching():
    route('shadow')
    events = emit(7, 'mqtt', sensor='sensor-02')
    expired = api('/commands', {'target': 'fan', 'desiredState': False, 'ttl': 1})
    queued = api('/commands', {'target': 'safetyGroup', 'desiredState': False, 'ttl': 120})
    received = {e['eventId'] for _, e in Client('shore').records('shore/anchor/events')}
    assert not received.intersection(e['eventId'] for e in events)
    subprocess.run([sys.executable, '-m', 'anchor.cli', 'restart', 'service'], check=True, capture_output=True)
    time.sleep(1.5)
    route('offshore')
    assert_delivered(events)
    eventually(lambda: result(expired)['status'] == 'expired')
    eventually(lambda: result(queued)['status'] == 'executed')
    with pytest.raises(OneM2MError) as missing:
        Client('ship').get('ship/fan/commands/' + record_name(expired['commandId']))
    assert missing.value.rsc == 4004
    before = api('/status')['batchNotifications']
    assert_delivered(emit(12, 'mqtt', sensor='sensor-03'))
    eventually(lambda: api('/status')['batchNotifications'] > before)
    # Orderly CSE restart must preserve accepted source data.
    route('shadow')
    pending = emit(3, 'http', sensor='sensor-04')
    subprocess.run([sys.executable, '-m', 'anchor.cli', 'restart', 'ship'], check=True, capture_output=True)
    route('coastal')
    assert_delivered(pending)


def test_discovery_paginates_beyond_one_page():
    route('offshore')
    boot = uuid.uuid4().hex
    events = emit(103, 'http', sensor='sensor-04', boot=boot)
    records = Client('ship').records('ship/sensor-04/history')
    assert {e['eventId'] for e in events}.issubset(e['eventId'] for _, e in records)
    assert_delivered(events)
    eventually(lambda: api('/status')['metrics']['complete'])


def test_real_shore_outage_keeps_local_safety_and_recovers():
    from anchor.lab.stack import start_node, stop_node
    route('offshore')
    stop_node('shore')
    try:
        eventually(lambda: api('/status')['state']['phase'] == 'DEGRADED')
        event = emit(1, 'mqtt', sensor='sensor-03', temperature=75, priority='critical')[0]
        name = record_name('local-' + record_name(event['eventId']))
        eventually(lambda: json.loads(resource(Client('ship').get(
            'ship/anchor/commandResults/' + name))['con'])['status'] == 'executed')
        assert not api('/status')['metrics']['complete']
        for device in ('fan', 'buzzer'):
            assert resource(Client('ship').get(f'ship/{device}/reportedState'))['state'] is True
        assert resource(Client('ship').get('ship/sensor-03/measurement'))['curT0'] == 75.0
    finally:
        start_node('shore')
    assert_delivered([event])
    eventually(lambda: api('/status')['metrics']['complete'])
