"""Bootstrap-only DDS IPE policies; the running IPE cannot administer the CSE."""
from ..resources import ae, assign_policy, container, resource_policy

ORIGIN = 'CAnchorDDSIPE'


def provision(client):
    admin = client.as_origin('CAdmin')
    ae(admin, 'ddsIpe', ORIGIN)
    readers = [([ORIGIN, 'CAnchorEdge', 'CAnchorDashboard'], 34)]
    p = resource_policy(admin, 'ddsIpe', readers)
    assign_policy(admin, admin.root + '/ddsIpe', 'm2m:ae', p, ORIGIN)
    for name in ('vibration', 'status'):
        path = 'ddsIpe/' + name
        # Only CIN creation; no commands, other sensor resources or ACP mutations.
        p = resource_policy(admin, path, readers + [([ORIGIN], 1, [{'chty': [4]}])])
        container(admin, admin.root + '/ddsIpe', name, p)
    return {'ae': 'ddsIpe', 'origin': ORIGIN, 'containers': ['vibration', 'status']}
