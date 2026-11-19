"""Policy boundaries and bindings from the main deployment."""
import json

import pytest

from anchor.client import resource
from anchor.config import Endpoint, ORIGINS, Settings
from anchor.provision import bootstrap
from dds_permissions import permission
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
