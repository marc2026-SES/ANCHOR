"""Send one time-limited reporting profile to both ship devices."""
import argparse
import json
import time
import uuid

from .client import Client, OneM2MError, canonical, resource
from .config import ORIGINS, Settings


def run(client, interval, duration, vessel):
    if not (200 <= interval <= 5000 and interval % 5 == 0 and 1000 <= duration <= 300000):
        raise ValueError('Interval must be 200..5000 ms in steps of 5, duration 1000..300000 ms')
    issued = time.time_ns() // 1000000
    command = dict(commandId=uuid.uuid4().hex, action='setReportProfile', target='reportingGroup',
                   issuer='shore', intervalMs=interval, durationMs=duration,
                   issuedAtMs=issued, expiresAtMs=issued + 30000)
    print(f"Sending {command['commandId']} to bridge and engine", flush=True)
    result = client.create(client.root + '/anchor/reportingGroup/fopt', 4, {'m2m:cin': {
        'rn': 'cmd-' + command['commandId'], 'cnf': 'application/json:0', 'con': canonical(command)}})
    replies = result.get('m2m:agr', {}).get('m2m:rsp', [])
    if len(replies) != 2 or any(reply.get('rsc') != 2001 for reply in replies):
        raise RuntimeError(f'Incomplete fan-out for {command["commandId"]}: {replies}')

    devices = ('bridge', 'engine')
    results = {}
    deadline = time.monotonic() + 30
    while len(results) < len(devices) and time.monotonic() < deadline:
        for device in devices:
            if device in results:
                continue
            try:
                con = resource(client.get(f'{client.root}/{device}/commandResults/la'))['con']
            except OneM2MError as exc:
                if exc.rsc == 4004:
                    continue
                raise
            row = json.loads(con) if isinstance(con, str) else con
            if not isinstance(row, dict) or row.get('commandId') != command['commandId']:
                continue
            if (row.get('deviceId') != device or row.get('vesselId') != vessel
                    or row.get('driver') != 'esp32' or row.get('command') != command):
                continue
            at = row.get('appliedAtMs')
            verified = (row.get('status') == 'applied' and row.get('appliedIntervalMs') == interval
                        and type(at) is int and issued <= at < command['expiresAtMs']
                        and row.get('profileUntilMs') == at + duration)
            results[device] = verified
            print(json.dumps({k: row.get(k) for k in ('deviceId', 'commandId', 'status',
                                                       'appliedIntervalMs', 'appliedAtMs', 'profileUntilMs')}), flush=True)
        if len(results) < len(devices):
            time.sleep(0.5)
    for device in devices:
        if device not in results:
            print(f'{device}: unconfirmed ({command["commandId"]})')
    return len(results) == 2 and all(results.values())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--interval-ms', type=int, default=500)
    parser.add_argument('--duration-ms', type=int, default=10000)
    args = parser.parse_args()
    settings = Settings.load(args.config)
    if settings.role != 'ship':
        parser.error('Use the ship role configuration')
    ship = Client(settings.nodes['ship'], ORIGINS['ship'])
    raise SystemExit(0 if run(ship, args.interval_ms, args.duration_ms, settings.vessel_id) else 1)
