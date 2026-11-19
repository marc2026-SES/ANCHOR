"""Resource provisioning helpers for the standalone DDS IPE."""
from .client import resource


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
