import argparse
import json
import os
import secrets
from pathlib import Path

import requests

from .config import Settings


def main(argv=None):
    parser = argparse.ArgumentParser(description='ANCHOR real hardware: ship / coastal edge / shore')
    parser.add_argument('--config', required=True, help='deploy/<ship|edge|shore>/core.json')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('serve', 'bootstrap', 'status', 'init-token', 'init-status-token'):
        sub.add_parser(name)
    command = sub.add_parser('command')
    command.add_argument('--target', choices=('bridge', 'engine', 'reportingGroup'), default='reportingGroup')
    command.add_argument('--interval-ms', type=int, default=200)
    command.add_argument('--duration-ms', type=int, default=30000)
    command.add_argument('--ttl-ms', type=int, default=60000)
    command.add_argument('--id')
    ack = sub.add_parser('ack')
    ack.add_argument('incident_id')
    ack.add_argument('--operator', required=True)
    for name in ('capture-batch', 'batch-status'):
        sub.add_parser(name).add_argument('batch_id')
    args = parser.parse_args(argv)
    settings = Settings.load(args.config)
    if args.command == 'serve':
        from .service import serve
        return serve(settings)
    if args.command == 'bootstrap':
        from .runtime import Runtime
        result = Runtime(settings).provision()
    elif args.command in ('init-token', 'init-status-token'):
        filename = settings.status_token_file if args.command == 'init-status-token' else settings.token_file
        if not filename:
            raise ValueError('Set status_token_file in the role config before initializing a status token')
        target = Path(filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write(secrets.token_urlsafe(32) + '\n')
        result = {'tokenFile': str(target), 'scope': 'status' if args.command == 'init-status-token' else 'operator'}
    else:
        path, body = '/status', None
        if args.command == 'command':
            path = '/commands'
            body = {'target': args.target, 'intervalMs': args.interval_ms,
                    'durationMs': args.duration_ms, 'ttlMs': args.ttl_ms}
            if args.id:
                body['commandId'] = args.id
        elif args.command == 'ack':
            path, body = '/incidents/ack', {'incidentId': args.incident_id, 'operator': args.operator}
        elif args.command == 'capture-batch':
            path, body = '/batches', {'batchId': args.batch_id}
        elif args.command == 'batch-status':
            from urllib.parse import quote
            path = '/batches/' + quote(args.batch_id, safe='')
        host = '127.0.0.1' if settings.bind == '0.0.0.0' else settings.bind
        from .auth import read_token
        # The local operator CLI keeps its full status view. External monitors
        # receive only status_token_file and use the restricted API response.
        token_file = settings.token_file
        with requests.Session() as session:
            session.trust_env = False
            response = session.request('POST' if body is not None else 'GET', f'https://{host}:{settings.port}{path}',
                                       json=body, timeout=15, verify=settings.tls_ca, allow_redirects=False,
                                       headers={'Authorization': 'Bearer ' + read_token(token_file)})
            if 300 <= response.status_code < 400:
                raise ValueError('API redirects are disabled')
            response.raise_for_status()
            result = response.json()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
