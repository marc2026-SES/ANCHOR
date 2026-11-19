"""Provision oneM2M access policies through the local CSE."""
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
    if any(result.get(key) != ruleset for key, ruleset in value.items()):
        admin.update(admin.root + '/' + name, {'m2m:acp': value})
    return result['ri']


def resource_policy(admin, path, grants):
    """One named policy per protected resource, relative to this CSEBase."""
    return policy(admin, 'acp-' + path.replace('/', '-'), grants)


def assign_policy(admin, path, tag, acpi, owner, current):
    if current.get('acpi') == [acpi]:
        return
    # Only the creator can assign the initial acpi.
    actor = admin if current.get('acpi') else admin.as_origin(owner)
    actor.update(path, {tag: {'acpi': [acpi]}})


def protected_container(admin, path, grants, owner='CAdmin'):
    acpi = resource_policy(admin, path, grants)
    full = admin.root + '/' + path
    parent, name = full.rsplit('/', 1)
    row = resource(admin.ensure(parent, 3, {'m2m:cnt': {'rn': name, 'acpi': [acpi]}}))
    assign_policy(admin, full, 'm2m:cnt', acpi, owner, row)
    return row
