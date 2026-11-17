#!/usr/bin/env python3
"""Issue local CA, three host bundles and separate ESP32 client credentials.

Run on the provisioning workstation. Never distribute issuer/ca.key to a Pi.
OpenSSL is the only dependency. Existing outputs are never overwritten.
"""
import argparse
import ipaddress
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess


def run(*args):
    result = subprocess.run(['openssl', *map(str, args)], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())


def san(value):
    try:
        return 'IP:' + str(ipaddress.ip_address(value))
    except ValueError:
        if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', value):
            raise ValueError(f'Invalid certificate hostname: {value}')
        return 'DNS:' + value


def provision(output, hosts):
    output = Path(output).resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    issuer = output / 'issuer'
    issuer.mkdir(mode=0o700)
    ca, ca_key = issuer / 'ca.crt', issuer / 'ca.key'
    run('req', '-x509', '-newkey', 'rsa:3072', '-nodes', '-sha256', '-days', '3650',
        '-subj', '/CN=ANCHOR Local Root CA', '-keyout', ca_key, '-out', ca,
        '-addext', 'basicConstraints=critical,CA:TRUE,pathlen:0',
        '-addext', 'keyUsage=critical,keyCertSign,cRLSign', '-addext', 'subjectKeyIdentifier=hash')
    ca_key.chmod(0o600)

    def issue(directory, name, cn, names=None):
        directory.mkdir(mode=0o700, exist_ok=True)
        cert, key = directory / (name + '.crt'), directory / (name + '.key')
        csr, ext = directory / (name + '.csr'), directory / (name + '.ext')
        eku = 'serverAuth,clientAuth' if names else 'clientAuth'
        extensions = f'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage={eku}\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n'
        if names:
            extensions += 'subjectAltName=' + ','.join(san(h) for h in dict.fromkeys(names)) + '\n'
        ext.write_text(extensions)
        run('req', '-new', '-newkey', 'rsa:2048', '-nodes', '-sha256', '-subj', '/CN=' + cn,
            '-keyout', key, '-out', csr)
        key.chmod(0o600)
        run('x509', '-req', '-in', csr, '-CA', ca, '-CAkey', ca_key,
            '-set_serial', '0x' + secrets.token_hex(16), '-days', '365', '-sha256',
            '-extfile', ext, '-out', cert)
        shutil.copyfile(ca, directory / 'ca.crt')
        csr.unlink(); ext.unlink()
        return cert, key

    for role in ('ship', 'edge', 'shore'):
        issue(output / role, 'server', role + '.local',
              [role + '.local', 'localhost', '127.0.0.1', '::1', *hosts.get(role, [])])
    firmware = output / 'firmware'
    firmware.mkdir(mode=0o700)
    for device in ('bridge', 'engine', 'cargo'):
        cert, key = issue(issuer / device, 'client', 'C' + device.title())
        header = '#pragma once\n// Private credentials: flash only into this device; never commit.\n'
        for symbol, source in [('CA', ca), ('CERT', cert), ('KEY', key)]:
            header += f'static const char ANCHOR_TLS_{symbol}[] = R"PEM({source.read_text()})PEM";\n'
        target = firmware / f'anchor_{device}_tls.h'
        target.write_text(header)
        target.chmod(0o600)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    for role in ('ship', 'edge', 'shore'):
        parser.add_argument('--' + role + '-host', action='append', default=[],
                            help='Additional DNS name or IP SAN (repeatable)')
    args = parser.parse_args()
    os.umask(0o077)
    output = provision(args.output, {r: getattr(args, r + '_host') for r in ('ship', 'edge', 'shore')})
    print(f'Created {output}. Copy only <role>/ to that Pi and firmware/ headers to the build machine.')
    print('Keep issuer/ offline. Certificates expire in 365 days; reissue before expiry.')


if __name__ == '__main__':
    main()
