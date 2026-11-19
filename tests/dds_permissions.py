import copy

import pytest

from anchor.client import resource


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
