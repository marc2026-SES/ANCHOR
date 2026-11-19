"""Real TLS handshakes via MemoryBIO: no network permissions required."""
import importlib.util
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tarfile
from unittest.mock import Mock

import pytest
import paho.mqtt.client as mqtt

from anchor.acme_tls import VerifiedMQTTClient, verified_request
from anchor.client import Client, resource
from anchor.config import Endpoint, Settings
from anchor.tls import client_context, server_context, require_https

ROOT = Path(__file__).resolve().parents[1]


def script(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def pki(tmp_path_factory):
    path = tmp_path_factory.mktemp('tls') / 'pki'
    return script('provision-tls').provision(path, {'ship': ['boat.example', '192.168.4.10']})


def handshake(server_ctx, client_ctx, hostname):
    si, so, ci, co = (ssl.MemoryBIO() for _ in range(4))
    server = server_ctx.wrap_bio(si, so, server_side=True)
    client = client_ctx.wrap_bio(ci, co, server_hostname=hostname)
    done = [False, False]
    for _ in range(100):
        for i, peer in enumerate((server, client)):
            if not done[i]:
                try:
                    peer.do_handshake()
                    done[i] = True
                except ssl.SSLWantReadError:
                    pass
        if so.pending:
            ci.write(so.read())
        if co.pending:
            si.write(co.read())
        if all(done):
            client.write(b'oneM2M request')
            si.write(co.read())
            assert server.read() == b'oneM2M request'
            return server, client
    raise AssertionError('TLS handshake did not converge')


def ship_context(pki, mtls=False):
    context = server_context(pki / 'ship/server.crt', pki / 'ship/server.key')
    if mtls:
        context.load_verify_locations(pki / 'ship/ca.crt')
        context.verify_mode = ssl.CERT_REQUIRED
    return context


@pytest.mark.parametrize('hostname', ['ship.local', 'localhost', '127.0.0.1', 'boat.example', '192.168.4.10'])
def test_https_valid_ca_and_san(pki, hostname):
    server, client = handshake(ship_context(pki), client_context(pki / 'ship/ca.crt'), hostname)
    assert client.version() in ('TLSv1.2', 'TLSv1.3')


def test_https_rejects_wrong_hostname_and_unknown_ca(pki):
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(ship_context(pki), client_context(pki / 'ship/ca.crt'), 'attacker.local')
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(ship_context(pki), client_context(pki / 'edge/server.crt'), 'ship.local')


@pytest.mark.parametrize('device', ['bridge', 'engine', 'cargo'])
def test_mqtt_mutual_tls_and_hostname_verification(pki, monkeypatch, device):
    monkeypatch.setenv('ANCHOR_TLS_DIR', str(pki / 'ship'))
    client = VerifiedMQTTClient(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    client.tls_set(ca_certs=str(pki / 'ship/ca.crt'),
                   certfile=str(pki / 'issuer' / device / 'client.crt'),
                   keyfile=str(pki / 'issuer' / device / 'client.key'))
    client.tls_insecure_set(True)  # pinned ACME's call must not disable hostname checking
    server, _ = handshake(ship_context(pki, True), client._ssl_context, 'ship.local')
    assert server.getpeercert()['subject'][0][0][1] == 'C' + device.title()
    with pytest.raises(ssl.SSLCertVerificationError):
        handshake(ship_context(pki, True), client._ssl_context, 'wrong.local')


def test_mqtt_rejects_missing_client_certificate_and_plaintext(pki):
    with pytest.raises(ssl.SSLError):
        handshake(ship_context(pki, True), client_context(pki / 'ship/ca.crt'), 'ship.local')
    with pytest.raises(ValueError, match='require TLS'):
        VerifiedMQTTClient(callback_api_version=mqtt.CallbackAPIVersion.VERSION2).connect_async('127.0.0.1', 1883)


def test_issuance_never_overwrites_keys_and_separates_devices(pki):
    before = (pki / 'issuer/ca.key').read_bytes()
    with pytest.raises(FileExistsError):
        script('provision-tls').provision(pki, {})
    assert (pki / 'issuer/ca.key').read_bytes() == before
    for role in ('ship', 'edge', 'shore'):
        assert not (pki / role / 'ca.key').exists()
        assert (pki / role / 'server.key').stat().st_mode & 0o077 == 0
    keys = [(pki / 'issuer' / device / 'client.key').read_bytes() for device in ('bridge', 'engine', 'cargo')]
    assert len(set(keys)) == 3
    for i, device in enumerate(('bridge', 'engine', 'cargo')):
        header = (pki / 'firmware' / f'anchor_{device}_tls.h').read_bytes()
        assert keys[i] in header
        assert all(key not in header for j, key in enumerate(keys) if j != i)


@pytest.mark.parametrize('role', ['ship', 'edge', 'shore'])
def test_deployment_configs_resolve_tls_and_reject_downgrade(tmp_path, role):
    path = ROOT / 'deploy' / role / 'host.json'
    cfg = Settings.load(path)
    assert cfg.role == role
    assert Path(cfg.tls_key) == ROOT / ('.runtime-' + role) / 'tls/server.key'
    assert cfg.nodes[role].url == 'https://127.0.0.1:8080'
    data = json.loads(path.read_text())
    data['nodes'][role]['url'] = 'http://127.0.0.1:8080'
    invalid = tmp_path / 'invalid.json'; invalid.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='HTTPS'):
        Settings.load(invalid)


@pytest.mark.parametrize('bad_ca', [False, '', 0, None])
def test_no_certificate_verification_bypass(bad_ca):
    with pytest.raises(ValueError):
        Endpoint('https://ship.local', 'cse-ship', '/id-ship', ca=bad_ca)


def test_http_clients_disable_redirects(pki, monkeypatch):
    response = Mock(status_code=302)
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=None)
    session.request.return_value = response
    monkeypatch.setattr('requests.Session', lambda: session)
    monkeypatch.setenv('ANCHOR_TLS_DIR', str(pki / 'ship'))
    with pytest.raises(Exception, match='redirects'):
        verified_request('GET', 'https://edge.local:8080', verify=False)
    kwargs = session.request.call_args.kwargs
    assert kwargs['verify'] == str(pki / 'ship/ca.crt') and kwargs['allow_redirects'] is False
    with pytest.raises(ValueError, match='redirects'):
        Client(Endpoint('https://ship.local', 'cse-ship', '/id-ship'), 'CBridge').get('cse-ship')
    for url in ('http://ship.local', 'https://user:password@ship.local'):
        with pytest.raises(ValueError):
            require_https(url)


@pytest.mark.parametrize('role', ['ship', 'edge', 'shore'])
def test_role_archive_contains_only_its_deployment_and_no_secrets(tmp_path, role):
    module = script('package-role')
    archive = tmp_path / (role + '.tgz')
    module.package(role, archive)
    with tarfile.open(archive) as handle:
        names = handle.getnames()
        assert handle.extractfile('.anchor-role').read().decode().strip() == role
        assert f'deploy/{role}/run.sh' in names
        assert 'src/anchor/acme_tls.py' in names
        assert 'src/anchor/runtime.py' not in names
        for other in {'ship', 'edge', 'shore'} - {role}:
            assert not any(name.startswith(f'deploy/{other}/') for name in names)
        assert not any(name.endswith(('.key', '.pem', '.crt', '_tls.h')) for name in names)
        assert not any('/lab/' in name or name.startswith('.runtime') for name in names)
        if role != 'ship':
            assert not any(name.startswith('hardware/firmware/') for name in names)
    with pytest.raises(FileExistsError):
        module.package(role, archive)


def test_role_launcher_rejects_cross_role_config_and_command(monkeypatch, tmp_path):
    launcher = script('role')
    with pytest.raises(ValueError, match='Unsupported action'):
        launcher.main(['ship', 'command'])
    with pytest.raises(ValueError, match='no sensor'):
        launcher.main(['edge', 'dashboard'])
    (tmp_path / '.anchor-role').write_text('shore\n')
    monkeypatch.setattr(launcher, 'ROOT', tmp_path)
    with pytest.raises(ValueError, match='different role'):
        launcher.prepare('ship')


def test_pinned_acme_adapter_installs_without_changing_upstream():
    pytest.importorskip('acmecse')
    result = subprocess.run([sys.executable, '-c',
        'from anchor.acme_tls import install_adapter; install_adapter(); install_adapter()'],
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
