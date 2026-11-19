import json
import os
import socket
import subprocess
import sys
import time

import pytest
import requests

from anchor import group_demo, sdt
from anchor.client import Client, OneM2MError, resource
from anchor.config import Settings, Endpoint
from anchor.provision import bootstrap
from tests.test_tls_deployment import pki


def test_invalid_samples_and_coalescing():
    assert sdt.sample_values(sdt.GPS, dict(fix=False, gps='ok', lat=0, lon=0)) is None
    assert sdt.sample_values(sdt.GPS, dict(fix=True, gps='ok', lat=91, lon=0)) is None
    assert sdt.sample_values(sdt.VIBRATION, dict(vib_rms_g=-1)) is None
    assert sdt.sample_values(sdt.TEMPERATURE, dict(temp_c=True, ok=True)) is None
    assert sdt.sample_values(sdt.TEMPERATURE, dict(temp_c=23, ok=False)) is None
    worker = sdt.LatestSDT(None)
    for stamp in ('20261119T120000', '20261119T120002', '20261119T120001'):
        worker.offer(dict(deviceId='cargo', sensor='climate', payload=dict(temp_c=22, ok=True), source={'ct': stamp}))
    assert worker.latest == {sdt.TEMPERATURE: ('20261119T120002', {'curT0': 22})}


@pytest.mark.skipif(os.getenv('ANCHOR_TLS_WIRE_INTEGRATION') != '1', reason='local ACME HTTPS integration')
def test_ship_demo_with_real_cse_and_simulated_device_reports(tmp_path, pki, monkeypatch):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    (tmp_path/'acme.ini').write_text(f"""[basic.config]
cseType=MN
cseID=id-ship
cseName=cse-ship
cseHost=127.0.0.1
networkInterface=127.0.0.1
httpPort={port}
databaseType=tinydb
adminID=CAdmin
[cse]
enableRemoteCSE=false
[cse.security]
enableACPChecks=true
[resource.cnt]
enableLimits=false
[http.security]
useTLS=true
verifyCertificate=true
caCertificateFile={pki/'ship/server.crt'}
caPrivateKeyFile={pki/'ship/server.key'}
[mqtt]
enable=false
[textui]
enable=false
[webui]
enable=false
""")
    ep = Endpoint(f'https://127.0.0.1:{port}', 'cse-ship', '/id-ship', ca=str(pki/'ship/ca.crt'))
    ship = Client(ep, 'CAnchorShip')
    with (tmp_path/'acme.log').open('w') as log:
        proc = subprocess.Popen([sys.executable, '-m', 'anchor.acme_tls', '--headless', '--base-directory',
                                 str(tmp_path), '--config', 'acme.ini'], cwd=tmp_path, stdout=log,
                                stderr=subprocess.STDOUT, env=dict(os.environ, ANCHOR_TLS_DIR=str(pki/'ship')))
        try:
            for _ in range(100):
                try:
                    ship.as_origin('CAdmin').get(ship.root)
                    break
                except (requests.ConnectionError, OneM2MError):
                    if proc.poll() is not None:
                        pytest.fail((tmp_path/'acme.log').read_text()[-1000:])
                    time.sleep(.1)
            bootstrap(Settings('ship', {'ship': ep}), ship)
            assert sdt.read_observed(ship) == {}
            projector = sdt.LatestSDT(ship)
            stamp = '20261119T140000,100000'
            for model, values in [(sdt.GPS, {'latie': 35.1, 'longe': 129.1}),
                                  (sdt.VIBRATION, {'numVe': .035}),
                                  (sdt.TEMPERATURE, {'curT0': 23.8})]:
                projector.publish(model, stamp, values)
                projector.publish(model, '20261119T140002', values)
            results = sdt.read_observed(ship)
            assert set(results) == {m.path for m in sdt.MODELS.values()}
            assert all(row['sampleTime'] == stamp for row in results.values())
            assert results[sdt.VIBRATION.path]['values'] == {'numVe': .035}
            with pytest.raises(OneM2MError) as denied:
                ship.as_origin('CAnchorDashboard').get('cse-ship/bridge/shipLocation')
            assert denied.value.rsc == 4103

            create = ship.create
            def simulate_devices(parent, ty, body):
                assert parent.endswith('/reportingGroup/fopt')
                response = create(parent, ty, body)
                for device in ('bridge', 'engine'):
                    node = ship.as_origin('C' + device.title())
                    path = f'{ship.root}/{device}'
                    cmd = json.loads(resource(node.get(path + '/command/la'))['con'])
                    at = cmd['issuedAtMs'] + 5
                    row = dict(command=cmd, commandId=cmd['commandId'], deviceId=device,
                               vesselId='ship-001', driver='esp32', status='applied', appliedAtMs=at,
                               appliedIntervalMs=cmd['intervalMs'], profileUntilMs=at + cmd['durationMs'])
                    node.create(path + '/commandResults', 4, {'m2m:cin': {'con': json.dumps(row)}})
                return response
            monkeypatch.setattr(ship, 'create', simulate_devices)
            assert group_demo.run(ship, 500, 10000, 'ship-001')
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
