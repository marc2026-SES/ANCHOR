"""Commands are complete only when real device result CINs have arrived."""
import time
import uuid

from .client import OneM2MError, canonical, resource
from .events import digest, record_name


def validate(command):
    if not isinstance(command, dict) or command.get('action') != 'setReportProfile':
        raise ValueError('Only setReportProfile is supported')
    if command.get('target') not in ('bridge', 'engine', 'reportingGroup'):
        raise ValueError('Target must be bridge, engine or reportingGroup')
    if not isinstance(command.get('commandId'), str) or not 1 <= len(command['commandId']) <= 64:
        raise ValueError('Invalid commandId')
    if command.get('issuer') != 'shore':
        raise ValueError('Invalid issuer')
    for key in ('issuedAtMs', 'expiresAtMs', 'durationMs', 'intervalMs'):
        if type(command.get(key)) is not int:
            raise ValueError(f'{key} must be an integer')
    if not 200 <= command['intervalMs'] <= 5000 or command['intervalMs'] % 5:
        raise ValueError('intervalMs must be 200..5000 in 5 ms steps')
    if not 1000 <= command['durationMs'] <= 300000:
        raise ValueError('durationMs must be 1000..300000')
    if not 0 < command['expiresAtMs'] - command['issuedAtMs'] <= 3600000:
        raise ValueError('Command TTL must be 1..3600000 ms')
    canonical(command)


def make(body, now=None):
    now = int(time.time() * 1000) if now is None else now
    command = {'commandId': body.get('commandId', uuid.uuid4().hex), 'action': 'setReportProfile',
               'target': body.get('target', 'reportingGroup'), 'issuer': 'shore',
               'issuedAtMs': now, 'expiresAtMs': now + body.get('ttlMs', 60000),
               'durationMs': body.get('durationMs', 30000), 'intervalMs': body.get('intervalMs', 200)}
    validate(command)
    return command


def targets(command):
    return ('bridge', 'engine') if command['target'] == 'reportingGroup' else (command['target'],)


def dispatch(client, journal, command, now_ms, results):
    validate(command)
    command_id = command['commandId']
    if all((command_id, device) in results for device in targets(command)):
        return 'reported'
    if now_ms >= command['expiresAtMs']:
        # A missing device response cannot prove that an earlier dispatch did not execute.
        status = 'expired-unconfirmed' if journal.get('dispatches', command_id) else 'expired'
        return status
    if now_ms < command['issuedAtMs']:
        return 'not-yet-valid'
    name = record_name(command_id)
    if command['target'] == 'reportingGroup':
        try:
            response = client.create(client.root + '/anchor/reportingGroup/fopt', 4,
                                     {'m2m:cin': {'rn': name, 'cnf': 'application/json:0', 'con': canonical(command)}})
        except OneM2MError as exc:
            # ACME can abort the whole fanout at a member that already has this CIN.
            # Reconcile every member below, including comparing any existing body.
            if exc.rsc != 4105:
                raise
        else:
            replies = response.get('m2m:agr', {}).get('m2m:rsp', [])
            if len(replies) != len(targets(command)) or any(r.get('rsc') not in (2001, 4105) for r in replies):
                raise RuntimeError(f'Incomplete group fanout: {response}')
    for device in targets(command):
        parent = client.root + '/' + device + '/command'
        client.put_record(parent, name, command)  # compare even after fanout 4105
    journal.put('dispatches', command_id, {'commandId': command_id, 'commandHash': digest(command),
                                         'status': 'dispatched', 'devices': list(targets(command))})
    return 'dispatched'


def result_index(events, expected_commands=()):
    expected = {c['commandId']: c for c in expected_commands}
    values = {}
    for event in events:
        if event['kind'] != 'commandResult':
            continue
        payload = event['payload']
        key = (payload.get('commandId'), event['deviceId'])
        command = expected.get(key[0])
        if command is None or event['deviceId'] not in targets(command) or payload.get('command') != command:
            continue
        if payload.get('driver') != 'esp32' or payload.get('status') not in ('applied', 'expired', 'rejected', 'superseded'):
            continue
        if payload['status'] == 'applied':
            applied = payload.get('appliedAtMs')
            if (type(applied) is not int or not command['issuedAtMs'] <= applied < command['expiresAtMs'] or
                payload.get('appliedIntervalMs') != command['intervalMs'] or
                payload.get('profileUntilMs') != applied + command['durationMs']):
                continue
        if key not in values or event['generatedAt'] > values[key]['generatedAt']:
            values[key] = event
    return values
