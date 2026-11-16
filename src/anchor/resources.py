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
    value = {'pv': {'acr': [{'acor': origins, 'acop': ops} for origins, ops in grants]},
             'pvs': {'acr': [{'acor': ['CAdmin'], 'acop': 63}]}}
    result = resource(admin.ensure(admin.root, 1, {'m2m:acp': {'rn': name, **value}}))
    admin.update(admin.root + '/' + name, {'m2m:acp': value})
    return result['ri']


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
    local_policy = policy(admin, 'anchor-local', [([core], 63)])
    assign_policy(admin, admin.root + '/anchor', 'm2m:ae', local_policy, core)
    incoming = {'ship': [], 'edge': [ORIGINS['ship']], 'shore': [ORIGINS['edge'], ORIGINS['ship']]}[role]
    downstream = {'ship': [], 'edge': [ORIGINS['ship']], 'shore': [ORIGINS['edge'], ORIGINS['ship']]}[role]
    for name in JOURNALS:
        grants = [([core], 63)]
        if name == 'events' and incoming:
            grants.append((incoming, 35))
        if name in ('receipts', 'commands') and downstream:
            grants.append((downstream, 34))
        p = policy(admin, 'anchor-' + name, grants)
        container(admin, admin.root + '/anchor', name, p)
    if role in POLLERS:
        name, origin = POLLERS[role]
        ae(admin, name, origin)
        p = policy(admin, 'anchor-poll', [([core, origin], 63)])
        assign_policy(admin, admin.root + '/' + name, 'm2m:ae', p, origin)
        admin.as_origin(origin).ensure(admin.root + '/' + name, 15,
                                      {'m2m:pch': {'rn': 'pch', 'rqag': True}})
    if role == 'ship':
        members = []
        for device, origin in DEVICES.items():
            ae(admin, device, origin)
            p = policy(admin, 'sensor-' + device, [([core], 63), ([origin], 7), (['CAnchorDashboard'], 2)])
            assign_policy(admin, admin.root + '/' + device, 'm2m:ae', p, origin)
            admin.update(admin.root + '/' + device, {'m2m:ae': {'lbl': ['anchor:hardware', 'device:' + device]}})
            for sensor in SENSORS[device]:
                # Location is kept separate from the public sensor dashboard identity.
                sensor_policy = p if sensor != 'position' else policy(admin, 'sensor-location', [([core], 63), ([origin], 7)])
                container(admin, admin.root + '/' + device, sensor, sensor_policy,
                          ['anchor:sensor', 'device:' + device, 'sensor:' + sensor], owner=origin)
            if device == 'cargo':
                admin.ensure(admin.root + '/cargo', 28, {'cod:tempe': {
                    'rn': 'temperature', 'cnd': 'org.onem2m.common.moduleclass.temperature',
                    'curT0': 0.0, 'acpi': [p], 'lbl': ['anchor:sdt', 'quality:unobserved']}})
            if device in ('bridge', 'engine'):
                cp = policy(admin, 'command-' + device, [([core], 63), ([origin], 3)])
                cmd = container(admin, admin.root + '/' + device, 'command', cp, owner=origin)
                container(admin, admin.root + '/' + device, 'commandResults', p,
                          ['anchor:sensor', 'device:' + device, 'sensor:commandResults'], owner=origin)
                members.append(cmd['ri'])
        group = {'mt': 3, 'mnm': 2, 'mid': members}
        existing = resource(client.ensure(client.root + '/anchor', 9, {'m2m:grp': {'rn': 'reportingGroup', **group}}))
        if existing.get('mt') != 3:
            raise ValueError('reportingGroup must contain CNT members')
        # memberType is immutable (TS-0004 / ACME attribute policy ou=NP).
        client.update(client.root + '/anchor/reportingGroup', {'m2m:grp': {'mnm': 2, 'mid': members}})
    return {'role': role, 'cse': client.root, 'origin': core, 'journals': list(JOURNALS)}
