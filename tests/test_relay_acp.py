import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from anchor.client import OneM2MError

ROOT = Path(__file__).resolve().parents[1]
relay = runpy.run_path(str(ROOT / 'hardware/ship-gateway-pi5/sync/anchor_relay.py'))


def test_cin_create_never_preflights_and_revocation_clears_confirmation(monkeypatch):
    response = SimpleNamespace(status_code=201, headers={'X-M2M-RSC': '2001'}, content=b'')
    attempts = 0

    def send(session, method, url, **kwargs):
        nonlocal attempts
        attempts += 1
        if method == 'GET':
            assert url.endswith('/cse-in/anchor/events/duplicate')
            return SimpleNamespace(status_code=200, headers={'X-M2M-RSC': '2000'}, content=b'json',
                                   json=lambda: {'m2m:cin': {'con': '{}'}})
        assert method == 'POST' and url.endswith('/cse-in/anchor/events')
        assert kwargs['headers']['X-M2M-Origin'] == 'CAnchorShip'
        assert kwargs['headers']['Content-Type'].endswith('ty=4')
        return response

    monkeypatch.setattr(requests.Session, 'request', send)
    config = relay['Config'](ROOT / 'deploy/ship/relay.json')
    sender = relay['Relay'](config, ':memory:')
    assert not sender.events_create_confirmed
    for n in range(10000):
        assert sender.forward_record(str(n), '{}') == 'forwarded'
    assert attempts == 10000 and sender.events_create_confirmed

    for http, headers in [(200, {'X-M2M-RSC': '4103'}), (403, {}), (401, {})]:
        response.status_code, response.headers = http, headers
        with pytest.raises(OneM2MError):
            sender.forward_record('pending', '{}')
        assert not sender.events_create_confirmed
        response.status_code, response.headers = 409, {'X-M2M-RSC': '4105'}
        assert sender.forward_record('duplicate', '{}') == 'duplicates'
        assert not sender.events_create_confirmed
        response.status_code, response.headers = 201, {'X-M2M-RSC': '2001'}
        assert sender.forward_record('pending', '{}') == 'forwarded'
        assert sender.events_create_confirmed
    assert attempts == 10012


@pytest.mark.parametrize('stored,remaining', [
    ({'m2m:cin': {'con': '{"seq":1}'}}, 0),
    ({'m2m:cin': {'con': '{"seq":2}'}}, 1),
    ({'m2m:cnt': {}}, 1),
    (OneM2MError(4004, {}, 'missing'), 1),
    (requests.Timeout(), 1),
])
def test_retry_verifies_only_the_named_cin_before_dequeue(monkeypatch, tmp_path, stored, remaining):
    config = relay['Config'](ROOT / 'deploy/ship/relay.json')
    queue_path = tmp_path / 'queue.sqlite'
    parent = 'cse-in/anchor/events'
    name, body = 'e-test', '{"seq":1}'
    sender = relay['Relay'](config, queue_path)
    sender.queue.put(name, body)
    requests_seen = []

    def lost_response(path, ty, payload):
        requests_seen.append(('CREATE', path))
        assert ty == 4 and payload['m2m:cin']['con'] == body
        raise requests.Timeout('response lost after storage')

    def run_one_pass(worker):
        monkeypatch.setattr(worker.stop, 'wait', lambda delay: worker.stop.set())
        monkeypatch.setattr(worker.wake, 'wait', lambda delay: worker.stop.set())
        worker.forward_loop()

    monkeypatch.setattr(sender.shore, 'create', lost_response)
    run_one_pass(sender)
    assert sender.queue.size() == 1
    sender.queue.db.close()

    def duplicate(path, ty, payload):
        requests_seen.append(('CREATE', path))
        assert payload['m2m:cin']['rn'] == name
        raise OneM2MError(4105, {}, path)

    def read_existing(path):
        requests_seen.append(('GET', path))
        if isinstance(stored, Exception):
            raise stored
        return stored

    restarted = relay['Relay'](config, queue_path)
    monkeypatch.setattr(restarted.shore, 'create', duplicate)
    monkeypatch.setattr(restarted.shore, 'get', read_existing)
    run_one_pass(restarted)
    assert requests_seen == [('CREATE', parent), ('CREATE', parent), ('GET', f'{parent}/{name}')]
    assert [(rn, con) for _, rn, con in restarted.queue.head(1)] == ([(name, body)] if remaining else [])
    assert restarted.stats['duplicates'] == 1 - remaining
    restarted.queue.db.close()
