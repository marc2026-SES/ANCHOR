#!/usr/bin/env python3
"""Fixed-role deployment entrypoint. No service starts another role's CSE."""
import configparser
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from anchor.config import Settings
from anchor.tls import client_context, server_context


def prepare(role):
    marker = ROOT / '.anchor-role'
    if marker.exists() and marker.read_text().strip() != role:
        raise ValueError('This deployment package belongs to a different role')
    config = ROOT / 'deploy' / role / 'host.json'
    settings = Settings.load(config)
    if settings.role != role:
        raise ValueError(f'{config}: role must be {role}')
    runtime = ROOT / ('.runtime-' + role)
    tls = runtime / 'tls'
    os.environ['ANCHOR_TLS_DIR'] = str(tls)
    os.environ['REQUESTS_CA_BUNDLE'] = str(tls / 'ca.crt')
    dds_prefix = Path(os.environ.get('ANCHOR_DDS_PREFIX', ROOT / '.runtime-dds'))
    if dds_prefix.exists():
        os.environ.setdefault('CYCLONEDDS_HOME', str(dds_prefix))
    return config, settings, runtime


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2 or argv[0] not in ('ship', 'edge', 'shore'):
        raise ValueError('Usage: deploy/<role>/run.sh check|init-cse|cse|dashboard|dds|dds-check|dds-provision')
    role, action, *rest = argv
    config, settings, runtime = prepare(role)
    python = Path(os.environ.get('ANCHOR_VENV', ROOT / '.venv')) / 'bin/python'
    if action == 'check':
        server_context(settings.tls_cert, settings.tls_key)
        client_context(settings.tls_ca, settings.tls_cert, settings.tls_key)
        print(json.dumps({'role': role, 'config': str(config), 'tls': 'loaded',
                          'cseDirectory': str(runtime / 'cse'),
                          'processes': ['cse'] + ([] if role == 'edge' else ['dashboard'])
                          + (['mosquitto', 'chrony'] if role == 'ship' else [])}, indent=2))
        return
    if action == 'init-cse':
        runtime.mkdir(mode=0o700, exist_ok=True)
        target = runtime / 'cse'
        # Explicitly initialize once. Never replace a pre-existing DB/config directory.
        target.mkdir(mode=0o700, exist_ok=False)
        shutil.copyfile(ROOT / 'deploy' / role / 'cse.ini', target / 'acme.ini')
        print(f'Initialized {target}. Existing DBs must be backed up and migrated before starting.')
        return
    if action == 'cse':
        target = runtime / 'cse'
        parser = configparser.ConfigParser(interpolation=None)
        if not parser.read(target / 'acme.ini'):
            raise ValueError(f'Missing {target}/acme.ini; run init-cse or migrate the existing CSE first')
        expected = {'ship': 'id-ship', 'edge': 'id-edge', 'shore': 'id-in'}[role]
        if parser.get('basic.config', 'cseID') != expected:
            raise ValueError('CSE identity does not match the selected role')
        if not parser.getboolean('cse.security', 'enableACPChecks', fallback=False):
            raise ValueError('CSE access control checks must be enabled')
        if not parser.getboolean('http.security', 'useTLS') or not parser.getboolean('http.security', 'verifyCertificate'):
            raise ValueError('CSE HTTPS and certificate verification are required')
        if parser.getboolean('mqtt', 'enable') and (not parser.getboolean('mqtt.security', 'useTLS') or
                                                   not parser.getboolean('mqtt.security', 'verifyCertificate')):
            raise ValueError('CSE MQTTS and certificate verification are required')
        os.chdir(target)
        command = [str(python), '-m', 'anchor.acme_tls', '--headless', '--base-directory', str(target), '--config', 'acme.ini', *rest]
    elif action in ('dds', 'dds-check', 'dds-provision'):
        if role == 'shore':
            raise ValueError('DDS runs on the ship publisher and coastal IPE only')
        if rest:
            raise ValueError('DDS deployment uses its fixed role config; extra arguments are not allowed')
        command = [str(python), '-m', 'anchor.dds.cli', '--config',
                   str(ROOT / 'deploy' / role / 'dds.json'),
                   {'dds': 'run', 'dds-check': 'check', 'dds-provision': 'provision'}[action]]
    elif action == 'dashboard':
        if role == 'edge':
            raise ValueError('Edge has no sensor/camera dashboard')
        relative = {'ship': 'ship-gateway-pi5/dashboard/server.py',
                    'shore': 'shore-server-pi4/shore-dashboard/shore_server.py'}[role]
        command = ['/usr/bin/python3', str(ROOT / 'hardware' / relative), *rest]
    else:
        raise ValueError(f'Unsupported action: {action}')
    os.execv(command[0], command)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, FileNotFoundError, FileExistsError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
