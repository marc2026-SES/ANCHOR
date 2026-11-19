#!/usr/bin/env python3
"""Produce one role's source archive; credentials and runtime state travel separately."""
import argparse
import fnmatch
import io
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE = ('*.key', '*.pem', '*.crt', 'anchor_*_tls.h', 'anchor_config.h', 'ota.env',
           '*.local.json', '*.local.ini', '.env', 'api-token', 'status-token', '*-status-token',
           '*.db*', '*.sqlite*', '*.pyc', '.DS_Store', '._*')
EXCLUDE_DIRS = {'__pycache__', 'data', 'logs', 'backups', 'build', 'lab'}


def package(role, destination):
    included = ['README.md', 'pyproject.toml', 'constraints-tested.txt', '.gitignore', '.dockerignore',
                'src', 'scripts', f'deploy/{role}', 'deploy/systemd']
    if role == 'ship':
        included += ['hardware/ship-gateway-pi5/dashboard', 'hardware/ship-gateway-pi5/sync', 'hardware/firmware']
    elif role == 'shore':
        included += ['hardware/shore-server-pi4/shore-dashboard']
    with open(destination, 'xb') as output, tarfile.open(fileobj=output, mode='w:gz') as archive:
        for relative in included:
            path = ROOT / relative
            for source in sorted(path.rglob('*')) if path.is_dir() else [path]:
                rel = source.relative_to(ROOT)
                if not source.is_file() or source.is_symlink() or set(rel.parts) & EXCLUDE_DIRS or source.resolve() == Path(destination).resolve():
                    continue
                if any(fnmatch.fnmatch(source.name, pattern) for pattern in EXCLUDE):
                    continue
                archive.add(source, arcname=str(rel), recursive=False)
        identity = (role + '\n').encode()
        info = tarfile.TarInfo('.anchor-role')
        info.size, info.mode = len(identity), 0o644
        archive.addfile(info, io.BytesIO(identity))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('role', choices=['ship', 'edge', 'shore'])
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    package(args.role, args.destination)
    print(f'{args.role} source archive: {args.destination}')
