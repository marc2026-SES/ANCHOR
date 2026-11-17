"""Provision the three independent CSEs without changing ACME itself."""
from .client import resource
from .config import DEVICES, ORIGINS, SENSORS

JOURNALS = ('events', 'receipts', 'commands', 'dispatches', 'quarantine', 'ruleState', 'audit')
POLLERS = {'edge': ('shipCommands', 'CAnchorShipPoll'), 'shore': ('edgeCommands', 'CAnchorEdgePoll')}


def ae(admin, name, origin, reachable=False):
    actor = admin.as_origin(origin)
    result = resource(actor.ensure(admin.root, 2, {'m2m:ae': {
        'rn': name, 'api': 'Nanchor.v2', 'rr': reachable, 'srv': ['3', '4']}}))
    if result['aei'] != origin:
        raise ValueError(f'AE {name} has a different owner')
    return result


def policy(admin, name, grants):
    rules = []
    for origins, ops, *conditions in grants:
        rule = {'acor': origins, 'acop': ops}
        if conditions:
            if len(conditions) != 1 or not conditions[0]:
                raise ValueError('Conditional grants require nonempty acod')
            rule['acod'] = conditions[0]
        rules.append(rule)
    value = {'pv': {'acr': rules},
             'pvs': {'acr': [{'acor': ['CAdmin'], 'acop': 63}]}}
    result = resource(admin.ensure(admin.root, 1, {'m2m:acp': {'rn': name, **value}}))
    admin.update(admin.root + '/' + name, {'m2m:acp': value})
    return result['ri']


def resource_policy(admin, path, grants):
    """One named policy per protected resource, relative to this CSEBase."""
    return policy(admin, 'acp-' + path.replace('/', '-'), grants)


def assign_policy(admin, path, tag, acpi, owner):
    current = resource(admin.get(path))
    # Initial acpi assignment is restricted to the creator even for CAdmin.
    actor = admin if current.get('acpi') else admin.as_origin(owner)
    actor.update(path, {tag: {'acpi': [acpi]}})


def container(admin, parent, name, acpi=None, labels=None, owner='CAdmin'):
    attrs = {'rn': name}
    if acpi:
        attrs['acpi'] = [acpi]
    if labels:
        attrs['lbl'] = labels
    result = resource(admin.ensure(parent, 3, {'m2m:cnt': attrs}))
    # Existing hardware CNTs had silent FIFO eviction. Preserve their contents,
    # remove the limits, then verify the CSE did not restore its defaults.
    if acpi:
        assign_policy(admin, parent + '/' + name, 'm2m:cnt', acpi, owner)
    admin.update(parent + '/' + name, {'m2m:cnt': {'mni': None, 'mbs': None, 'mia': None}})
    if labels:
        admin.update(parent + '/' + name, {'m2m:cnt': {'lbl': labels}})
    result = resource(admin.get(parent + '/' + name))
    if any(result.get(key) is not None for key in ('mni', 'mbs', 'mia')):
        raise RuntimeError('Set [resource.cnt] enableLimits=false in ACME and restart before bootstrap')
    return result


def bootstrap(settings, client):
    role = settings.role
    admin = client.as_origin('CAdmin')
    core = ORIGINS[role]
    ae(admin, 'anchor', core)
    # Register peer identities locally; they still only receive the grants below.
    for other, origin in ORIGINS.items():
        if other != role:
            ae(admin, 'peer-' + other, origin)
    ae(admin, 'dashboard', 'CAnchorDashboard')
    local_policy = resource_policy(admin, 'anchor', [([core], 63)])
    assign_policy(admin, admin.root + '/anchor', 'm2m:ae', local_policy, core)
    incoming = {'ship': [], 'edge': [ORIGINS['ship']], 'shore': [ORIGINS['edge'], ORIGINS['ship']]}[role]
    downstream = {'ship': [], 'edge': [ORIGINS['ship']], 'shore': [ORIGINS['edge'], ORIGINS['ship']]}[role]
    for name in JOURNALS:
        grants = [([core], 63)]
        if name == 'events' and incoming:
            grants.append((incoming, 35))
        if name in ('receipts', 'commands') and downstream:
            grants.append((downstream, 34))
        if role == 'shore' and name in ('events', 'receipts', 'commands'):
            grants.append((['CAnchorDashboard'], 34))
            if name == 'events':
                # A reader may subscribe, but may never forge a journal CIN.
                grants.append((['CAnchorDashboard'], 1, [{'chty': [23]}]))
        if role == 'edge' and settings.dashboard_edge_read and name in ('events', 'commands'):
            grants.append((['CAnchorDashboard'], 34))
        p = resource_policy(admin, 'anchor/' + name, grants)
        container(admin, admin.root + '/anchor', name, p)
    if role in POLLERS:
        name, origin = POLLERS[role]
        ae(admin, name, origin)
        p = resource_policy(admin, name, [([core, origin], 63)])
        assign_policy(admin, admin.root + '/' + name, 'm2m:ae', p, origin)
        admin.as_origin(origin).ensure(admin.root + '/' + name, 15,
                                      {'m2m:pch': {'rn': 'pch', 'rqag': True}})
    if role == 'ship':
        members = []
        for device, origin in DEVICES.items():
            ae(admin, device, origin)
            sensor_grants = [([core], 63), ([origin], 7), (['CAnchorDashboard'], 2)]
            p = resource_policy(admin, device, sensor_grants)
            assign_policy(admin, admin.root + '/' + device, 'm2m:ae', p, origin)
            admin.update(admin.root + '/' + device, {'m2m:ae': {'lbl': ['anchor:hardware', 'device:' + device]}})
            for sensor in SENSORS[device]:
                # Location is kept separate from the public sensor dashboard identity.
                grants = sensor_grants if sensor != 'position' else [([core], 63), ([origin], 7)]
                sensor_policy = resource_policy(admin, device + '/' + sensor, grants)
                container(admin, admin.root + '/' + device, sensor, sensor_policy,
                          ['anchor:sensor', 'device:' + device, 'sensor:' + sensor], owner=origin)
            if device == 'cargo':
                temperature_policy = resource_policy(admin, 'cargo/temperature', sensor_grants)
                admin.ensure(admin.root + '/cargo', 28, {'cod:tempe': {
                    'rn': 'temperature', 'cnd': 'org.onem2m.common.moduleclass.temperature',
                    'curT0': 0.0, 'acpi': [temperature_policy], 'lbl': ['anchor:sdt', 'quality:unobserved']}})
                # Also migrate an existing FCNT without resetting its observed value.
                assign_policy(admin, admin.root + '/cargo/temperature', 'cod:tempe', temperature_policy, 'CAdmin')
            if device in ('bridge', 'engine'):
                cp = resource_policy(admin, device + '/command',
                                     [([core], 63), ([origin], 2), ([origin], 1, [{'chty': [23]}])])
                cmd = container(admin, admin.root + '/' + device, 'command', cp, owner=origin)
                rp = resource_policy(admin, device + '/commandResults', sensor_grants)
                container(admin, admin.root + '/' + device, 'commandResults', rp,
                          ['anchor:sensor', 'device:' + device, 'sensor:commandResults'], owner=origin)
                members.append(cmd['ri'])
        group = {'mt': 3, 'mnm': 2, 'mid': members}
        existing = resource(client.ensure(client.root + '/anchor', 9, {'m2m:grp': {'rn': 'reportingGroup', **group}}))
        if existing.get('mt') != 3:
            raise ValueError('reportingGroup must contain CNT members')
        # memberType is immutable (TS-0004 / ACME attribute policy ou=NP).
        client.update(client.root + '/anchor/reportingGroup', {'m2m:grp': {'mnm': 2, 'mid': members}})
        gp = resource_policy(admin, 'anchor/reportingGroup', [([core], 63)])
        assign_policy(admin, admin.root + '/anchor/reportingGroup', 'm2m:grp', gp, core)
    return {'role': role, 'cse': client.root, 'origin': core, 'journals': list(JOURNALS)}
