"""ACP decisions use pinned ACME code; HTTP socket integration is tested separately."""
import copy
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from anchor.auth import ApiTokens
from anchor.client import resource
from anchor.config import Endpoint, ORIGINS, Settings
from anchor.resources import bootstrap
from fake_cse import MemoryCSE


@pytest.fixture
def cse_rows():
    servers = {}
    for role in ('ship', 'edge', 'shore'):
        endpoint = Endpoint('https://' + role, 'cse-' + role, '/id-' + role)
        cfg = Settings(role, {role: endpoint}, dashboard_edge_read=True)
        server = MemoryCSE(endpoint)
        bootstrap(cfg, server.client(ORIGINS[role]))
        servers[role] = (server, cfg)
    return servers


def permission(server, target, origin, operation, ty=None, self_permission=False):
    pytest.importorskip('acmecse')
    from acmecse.etc.Constants import Constants
    from acmecse.etc.Types import Permission, ResourceTypes
    from acmecse.resources.ACP import ACP
    from acmecse.services.SecurityManager import SecurityManager
    client = server.client()
    row = resource(client.get(server.endpoint.root + '/' + target))
    policy = resource(client.get(row['acpi'][0]))
    acp = ACP({k: copy.deepcopy(v) for k, v in policy.items() if not k.startswith('_')})
    acp.setAttribute(Constants.attrRiTyMapping, {})
    checker = object.__new__(SecurityManager)
    if self_permission:
        return checker.checkACPSelfPermission(acp, origin, Permission(operation))
    return checker.checkSingleACPPermission(acp, origin, Permission(operation), ResourceTypes(ty) if ty else None).allowed


@pytest.mark.parametrize('role,target,origin,op,ty,allowed', [
    ('ship','bridge/attitude','CAnchorDashboard',1,4,False),  # T1
    ('ship','bridge/position','CAnchorDashboard',2,None,False),  # T2
    ('ship','cargo/climate','CEngine',1,4,False),  # T3
    ('ship','bridge/command','CEngine',1,4,False),  # T4
    ('shore','anchor/events','CAnchorShip',8,None,False),  # T5, CIN inherits parent ACP
    ('shore','anchor/receipts','CAnchorEdge',4,None,False),  # T6
    ('shore','anchor/events','CAnchorDashboard',2,None,True),  # T8
    ('shore','anchor/events','CAnchorDashboard',32,None,True),
    ('shore','anchor/events','CAnchorDashboard',1,4,False),  # T9
    ('shore','anchor/receipts','CAnchorDashboard',1,23,False),  # T10
    ('shore','anchor/events','CAnchorDashboard',1,23,True),
    ('shore','anchor/events','CAnchorDashboard',4,None,False),
    ('shore','anchor/events','CAnchorDashboard',8,None,False),
    ('shore','anchor/commands','CAnchorDashboard',1,4,False),
    ('ship','bridge/command','CBridge',1,4,False),
    ('ship','bridge/command','CBridge',1,23,True),
    ('ship','engine/command','CEngine',1,4,False),
    ('ship','engine/command','CEngine',1,23,True),
    ('ship','bridge/command','CAnchorShip',1,4,True),
    ('edge','anchor/events','CAnchorDashboard',2,None,True),
    ('edge','anchor/events','CAnchorDashboard',1,23,False),
    ('edge','anchor/receipts','CAnchorDashboard',2,None,False),
    ('shore','anchor/events','CAnchorAgent',2,None,False),
    ('shore','anchor/events','CAnchorAgent',1,23,False),
])
def test_acp_operation_boundaries(cse_rows, role, target, origin, op, ty, allowed):
    assert permission(cse_rows[role][0], target, origin, op, ty) is allowed


@pytest.mark.parametrize('role', ['ship','edge','shore'])
def test_every_policy_self_privilege_is_admin_only(cse_rows, role):
    server, _ = cse_rows[role]
    policies = [r for r in server.rows.values() if r['ty'] == 1]
    assert policies
    assert all(r['rn'].startswith('acp-') for r in policies)
    assert all(r['pvs'] == {'acr':[{'acor':['CAdmin'],'acop':63}]} for r in policies)
    for origin in (ORIGINS[role], 'CAnchorDashboard', 'CBridge', 'CAnchorAgent'):
        assert not permission(server, 'anchor/events', origin, 4, self_permission=True)
    assert permission(server, 'anchor/events', 'CAdmin', 4, self_permission=True)


@pytest.mark.parametrize('target', [
    'bridge/attitude', 'bridge/position', 'engine/vibration',
    'engine/gas', 'cargo/climate', 'cargo/hatch',
])
def test_agent_can_only_read_and_discover_the_six_sensors(cse_rows, target):
    server, _ = cse_rows['ship']
    for op in (2, 32):
        assert permission(server, target, 'CAnchorAgent', op)
    for op, ty in ((1, 4), (1, 23), (4, None), (8, None), (16, None)):
        assert not permission(server, target, 'CAnchorAgent', op, ty)


def test_agent_grants_are_scoped_and_survive_rebootstrap(cse_rows):
    server, cfg = cse_rows['ship']
    client = server.client('CAnchorShip')
    client.put_record(client.root + '/bridge/position', 'agent-read-probe', {'lat': 35.0, 'lon': 129.0})
    bootstrap(cfg, client)
    bootstrap(cfg, client)
    assert resource(client.get(client.root + '/agent'))['aei'] == 'CAnchorAgent'
    assert json.loads(resource(client.get(client.root + '/bridge/position/agent-read-probe'))['con']) == {
        'lat': 35.0, 'lon': 129.0}
    allowed = {'agent', 'bridge', 'engine', 'cargo', 'bridge/attitude', 'bridge/position',
               'engine/vibration', 'engine/gas', 'cargo/climate', 'cargo/hatch'}
    for role, (node, _) in cse_rows.items():
        for path, row in node.rows.items():
            if not row.get('acpi'):
                continue
            target = path.split('/', 1)[1]
            expected = role == 'ship' and target in allowed
            for op in (2, 32):
                assert permission(node, target, 'CAnchorAgent', op) is expected, (role, target, op)
            for op, ty in ((1, 4), (1, 23), (4, None), (8, None), (16, None)):
                assert not permission(node, target, 'CAnchorAgent', op, ty), (role, target, op)
            assert not permission(node, target, 'CAnchorAgent', 4, self_permission=True)
    # GPS was authorized for the AI identity, not for the dashboard identity.
    assert not permission(server, 'bridge/position', 'CAnchorDashboard', 2)


def test_per_resource_policies_and_existing_fcnt_migration_preserve_data(cse_rows):
    server, cfg = cse_rows['ship']
    client = server.client('CAnchorShip')
    raw = client.put_record('cse-ship/cargo/climate', 'keep', {'temp_c': 24.7})
    assert raw
    client.update('cse-ship/cargo/temperature', {'cod:tempe': {'curT0': 24.7}})
    old = resource(client.create('cse-ship', 1, {'m2m:acp': {
        'rn':'sensor-cargo', 'pv':{'acr':[]}, 'pvs':{'acr':[{'acor':['CAdmin'],'acop':63}]}}}))
    client.as_origin('CAdmin').update('cse-ship/cargo/temperature', {'cod:tempe': {'acpi':[old['ri']]}})
    bootstrap(cfg, client)
    assert json.loads(resource(client.get('cse-ship/cargo/climate/keep'))['con']) == {'temp_c':24.7}
    fcnt = resource(client.get('cse-ship/cargo/temperature'))
    assert fcnt['curT0'] == 24.7
    assert resource(client.get(fcnt['acpi'][0]))['rn'] == 'acp-cargo-temperature'
    protected = [(p,r) for p,r in server.rows.items() if r.get('acpi') and r['ty'] in (2,3,9,28)]
    assert len({r['acpi'][0] for _,r in protected}) == len(protected)
    assert all(resource(client.get(r['acpi'][0]))['rn'] == 'acp-' + p.split('/',1)[1].replace('/','-') for p,r in protected)
    assert 'cse-ship/sensor-cargo' in server.rows  # no automatic destructive cleanup


def test_edge_read_permission_can_be_disabled_again(cse_rows):
    server, cfg = cse_rows['edge']
    cfg.dashboard_edge_read = False
    bootstrap(cfg, server.client('CAnchorEdge'))
    assert not permission(server, 'anchor/events', 'CAnchorDashboard', 2)


class RequestBytes:
    def __init__(self, raw):
        self.raw, self.sent = raw, bytearray()

    def makefile(self, *args):
        return io.BytesIO(self.raw)

    def sendall(self, data):
        self.sent.extend(data)


@pytest.fixture
def api(monkeypatch):
    from anchor import service, tls
    captured = {}
    def construct(address, handler):
        captured['handler'] = handler
        return SimpleNamespace()
    monkeypatch.setattr(service, 'ThreadingHTTPServer', construct)
    monkeypatch.setattr(tls, 'secure_server', lambda server, *_: server)
    state = {'role':'shore','ready':True,'lastScanAt':1,'delivery':{'accepted':1},
             'latest':{'bridge/position':{'payload':{'lat':35.0,'lon':129.0}}},
             'errors':[{'error':'private location payload'}],
             'links':{'edge':{'reachable':True,'observedAt':1,'pendingAtPeer':3,'error':'private diagnostic'}}}
    calls=[]
    runtime=SimpleNamespace(settings=SimpleNamespace(bind='127.0.0.1',port=8790,tls_cert='cert',tls_key='key'),
                            role='shore',status=lambda:copy.deepcopy(state),
                            submit_command=lambda body:calls.append(body) or body,
                            capture_batch=lambda body:calls.append(body) or body,
                            acknowledge=lambda *body:calls.append(body) or body)
    service.make_server(runtime, 'operator-'+'a'*32, 'status-'+'b'*32)
    def request(method='GET', path='/status', token='status-'+'b'*32, body=None):
        payload=json.dumps(body or {}).encode()
        raw=(f'{method} {path} HTTP/1.1\r\nHost: test\r\nConnection: close\r\nAuthorization: Bearer {token}\r\nContent-Length: {len(payload)}\r\n\r\n').encode()+payload
        connection=RequestBytes(raw)
        captured['handler'](connection, ('127.0.0.1',1), SimpleNamespace())
        headers,data=bytes(connection.sent).split(b'\r\n\r\n',1)
        return int(headers.split()[1]),json.loads(data)
    return request,calls


def test_monitor_reads_only_counters_and_cannot_get_location(api):
    request,_=api
    status,body=request()
    assert status==200 and body['delivery']['accepted']==1
    assert 'latest' not in body and 'errors' not in body
    assert body['links']=={'edge':{'reachable':True,'observedAt':1,'pendingAtPeer':3}}
    assert request(token='operator-'+'a'*32)[1]['latest']['bridge/position']['payload']['lat']==35.0


@pytest.mark.parametrize('path', ['/commands','/incidents/ack','/batches'])
def test_status_token_cannot_mutate(api,path):
    request,calls=api
    assert request('POST',path)[0]==403
    assert not calls


def test_tokenless_status_and_unknown_credentials_are_rejected(api):
    request,_=api
    assert request(token='')[0]==401  # T7
    assert request(token='wrong')[0]==401
    assert request('GET','/batches/private')[0]==403


def test_read_and_write_tokens_cannot_be_identical():
    with pytest.raises(ValueError,match='different'):
        ApiTokens('a'*32,'a'*32)


def test_status_token_creation_never_overwrites_or_prints_secrets(tmp_path,capsys):
    from anchor.cli import main
    data=json.loads((Path(__file__).parents[1]/'deploy/ship/core.json').read_text())
    target=tmp_path/'status-token'
    data['status_token_file']=str(target)
    config=tmp_path/'core.json';config.write_text(json.dumps(data))
    main(['--config',str(config),'init-status-token'])
    token=target.read_text().strip()
    assert len(token)>=24 and target.stat().st_mode & 0o777==0o600
    assert token not in capsys.readouterr().out
    with pytest.raises(FileExistsError):main(['--config',str(config),'init-status-token'])
    assert target.read_text().strip()==token
