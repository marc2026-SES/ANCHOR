"""Bootstrap-only DDS IPE policies; the running IPE cannot administer the CSE."""
from ..client import resource
from ..resources import ae, assign_policy, protected_container, resource_policy

ORIGIN = 'CAnchorDDSIPE'


def provision(client, *, configure_storage=True):
    admin = client.as_origin('CAdmin')
    row = ae(admin, 'ddsIpe', ORIGIN)
    readers = [([ORIGIN, 'CAnchorEdge', 'CAnchorDashboard'], 34)]
    p = resource_policy(admin, 'ddsIpe', readers)
    assign_policy(admin, admin.root + '/ddsIpe', 'm2m:ae', p, ORIGIN, row)
    for name in ('vibration', 'status'):
        path = 'ddsIpe/' + name
        # Only CIN creation; no commands, other sensor resources or ACP mutations.
        row = protected_container(admin, path, readers + [([ORIGIN], 1, [{'chty': [4]}])])
        limits = {'mni': None, 'mbs': None, 'mia': None}
        if configure_storage and any(row.get(key) is not None for key in limits):
            full = admin.root + '/' + path
            admin.update(full, {'m2m:cnt': limits})
            updated = resource(admin.get(full))
            if any(updated.get(key) is not None for key in limits):
                raise RuntimeError('Disable default container limits before DDS provisioning')
    return {'ae': 'ddsIpe', 'origin': ORIGIN, 'containers': ['vibration', 'status']}
