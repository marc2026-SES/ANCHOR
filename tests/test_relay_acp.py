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
    assert attempts == 10009
