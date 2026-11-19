"""Render a separate core handover scenario from the existing two-edge sandbox export."""
import argparse
import json
import os
from pathlib import Path
import uuid


def render(scenario, name, image, mirror_command=None):
    if not name or name == scenario['name']:
        raise ValueError('Use a distinct scenario name')
    hosts = {p['name']: p for domain in scenario['deployment']['domains'] for zone in domain['zones']
             for network in zone['networkLocations'] for p in network.get('physicalLocations', [])}
    for host, app in (('ship-001', 'anchor-ship-app'), ('mep1', 'anchor-edge-app'), ('mep2', 'anchor-edge-b-app')):
        processes = hosts[host]['processes']
        process = next((p for p in processes if p['name'] == app), None)
        if process is None:
            raise ValueError(f'Missing {app} on {host}: export the existing two-edge scenario')
        if host == 'ship-001' and mirror_command:
            # Only the existing mirror runs here. Do not start a second sensor sender.
            processes.append(dict(id=str(uuid.uuid4()), name='anchor-ship-mirror', type=process['type'],
                                  image=process['image'], dependencies=process.get('dependencies', []),
                                  environment=process['environment'], commandExe=mirror_command[0],
                                  commandArguments=','.join(mirror_command[1:])))
        process['image'] = image
        process.pop('commandExe', None)
        process.pop('commandArguments', None)
    scenario['name'] = name
    return scenario


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--name', default='anchor-core-handover')
    parser.add_argument('--image', required=True)
    mirror = parser.add_mutually_exclusive_group(required=True)
    mirror.add_argument('--mirror-command', nargs='+', help='Verified standalone mirror command in the original ship image')
    mirror.add_argument('--external-mirror', action='store_true', help='Sensor CINs already reach the ship mirror independently')
    args = parser.parse_args()
    model = render(json.loads(args.source.read_text()), args.name, args.image, args.mirror_command)
    # Exports may contain deployment credentials. Never print the environment or overwrite the source.
    with os.fdopen(os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w') as output:
        json.dump(model, output, indent=2)
        output.write('\n')
