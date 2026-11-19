"""Manually install the main deployment's ACPs and bind their target resources."""
import argparse

from .client import Client, resource
from .config import DEVICES, ORIGINS, SENSORS, Settings
from .resources import ae, assign_policy, protected_container, resource_policy


def protected_ae(admin, name, owner, grants):
    row = ae(admin, name, owner)
    policy = resource_policy(admin, name, grants)
    assign_policy(admin, admin.root + '/' + name, 'm2m:ae', policy, owner, row)


def ship_policies(admin, client, core):
    agent = 'CAnchorAgent'
    protected_ae(admin, 'agent', agent, [([agent], 34)])
    members = []
    for device, owner in DEVICES.items():
        sensor_grants = [([core], 63), ([owner], 7), (['CAnchorDashboard'], 2)]
        protected_ae(admin, device, owner, sensor_grants + [([agent], 34)])
        for sensor in SENSORS[device]:
            # GPS is private to the device, ship core, and explicitly authorized AI.
            grants = sensor_grants[:2] if sensor == 'position' else sensor_grants
            if (device, sensor) in AGENT_SENSORS:
                grants = grants + [([agent], 34)]
            protected_container(admin, f'{device}/{sensor}', grants, owner)
        if device in ('bridge', 'engine'):
            row = protected_container(admin, device + '/command',
                                      [([core], 63), ([owner], 2), ([owner], 1, [{'chty': [23]}])], owner)
            members.append(row['ri'])
            protected_container(admin, device + '/commandResults', sensor_grants, owner)
        if device == 'cargo':
            path = 'cargo/temperature'
            policy = resource_policy(admin, path, sensor_grants)
            row = resource(admin.ensure(admin.root + '/cargo', 28, {'cod:tempe': {
                'rn': 'temperature', 'cnd': 'org.onem2m.common.moduleclass.temperature',
                'curT0': 0.0, 'acpi': [policy], 'lbl': ['anchor:sdt', 'quality:unobserved']}}))
            assign_policy(admin, admin.root + '/' + path, 'cod:tempe', policy, 'CAdmin', row)
    path = 'anchor/reportingGroup'
    policy = resource_policy(admin, path, [([core], 63)])
    row = resource(client.ensure(client.root + '/anchor', 9, {'m2m:grp': {
        'rn': 'reportingGroup', 'mt': 3, 'mnm': 2, 'mid': members}}))
    if row.get('mt') != 3:
        raise ValueError('reportingGroup must contain CNT members')
    assign_policy(admin, admin.root + '/' + path, 'm2m:grp', policy, core, row)


# An explicit allowlist from main. Adding a sensor does not grant AI access to it.
AGENT_SENSORS = {('bridge', 'attitude'), ('bridge', 'position'), ('engine', 'vibration'),
                 ('engine', 'gas'), ('cargo', 'climate'), ('cargo', 'hatch')}


def bootstrap(settings, client):
    role = settings.role
    core = ORIGINS[role]
    admin = client.as_origin('CAdmin')
    protected_ae(admin, 'anchor', core, [([core], 63)])
    for peer, origin in ORIGINS.items():
        if peer != role:
            ae(admin, 'peer-' + peer, origin)
    ae(admin, 'dashboard', 'CAnchorDashboard')
    peers = {'ship': [], 'edge': [ORIGINS['ship']], 'shore': [ORIGINS['edge'], ORIGINS['ship']]}[role]
    for name in ('events', 'receipts', 'commands', 'dispatches', 'quarantine', 'ruleState', 'audit'):
        grants = [([core], 63)]
        if peers and name in ('events', 'receipts', 'commands'):
            grants.append((peers, 35 if name == 'events' else 34))
        if role == 'shore' and name in ('events', 'receipts', 'commands'):
            grants.append((['CAnchorDashboard'], 34))
            if name == 'events':
                grants.append((['CAnchorDashboard'], 1, [{'chty': [23]}]))
        if role == 'edge' and settings.dashboard_edge_read and name in ('events', 'commands'):
            grants.append((['CAnchorDashboard'], 34))
        protected_container(admin, 'anchor/' + name, grants)
    if role == 'ship':
        ship_policies(admin, client, core)
    else:
        name, origin = (('shipCommands', 'CAnchorShipPoll') if role == 'edge'
                        else ('edgeCommands', 'CAnchorEdgePoll'))
        protected_ae(admin, name, origin, [([core, origin], 63)])
    if settings.dds_ipe_enabled:
        from .dds.resources import provision
        provision(client, configure_storage=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    settings = Settings.load(parser.parse_args().config)
    bootstrap(settings, Client(settings.nodes[settings.role], ORIGINS[settings.role]))
