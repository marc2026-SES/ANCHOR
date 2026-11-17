"""Opt-in real Mosquitto + ACME: MQTTS uplink and command notification return path.

ANCHOR_TLS_WIRE_INTEGRATION=1 python -m pytest tests/test_mqtt_tls_integration.py -v
Requires loopback listeners, the pinned ACME package, openssl and mosquitto.
"""
import json
import os
from pathlib import Path
import queue
import shutil
import socket
import subprocess
import sys
import time

import paho.mqtt.client as mqtt
import pytest

from anchor.client import Client
from anchor.config import Endpoint
from anchor.tls import client_context
from tests.test_tls_deployment import pki

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get('ANCHOR_TLS_WIRE_INTEGRATION') != '1', reason='opt-in real MQTTS + HTTPS wire test')]


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def test_onem2m_uplink_and_downlink_over_mutual_tls(tmp_path, pki):
    broker_binary = shutil.which('mosquitto')
    if not broker_binary:
        pytest.fail('mosquitto is required for the requested wire test')
    mqtt_port, https_port = free_port(), free_port()
    broker_config = tmp_path / 'mosquitto.conf'
    broker_config.write_text(f'''listener {mqtt_port} 127.0.0.1
cafile {pki / 'ship/ca.crt'}
certfile {pki / 'ship/server.crt'}
keyfile {pki / 'ship/server.key'}
tls_version tlsv1.2
require_certificate true
use_identity_as_username true
allow_anonymous false
''')
    acme_dir = tmp_path / 'cse'; acme_dir.mkdir()
    (acme_dir / 'acme.ini').write_text(f'''[basic.config]
cseType=MN
cseID=id-ship
cseName=cse-ship
cseHost=127.0.0.1
httpPort={https_port}
networkInterface=127.0.0.1
databaseType=tinydb
adminID=CAdmin
[cse]
enableRemoteCSE=false
[http.security]
useTLS=true
verifyCertificate=true
caCertificateFile={pki / 'ship/server.crt'}
caPrivateKeyFile={pki / 'ship/server.key'}
[mqtt]
enable=true
address=127.0.0.1
port={mqtt_port}
[mqtt.security]
useTLS=true
verifyCertificate=true
caCertificateFile={pki / 'ship/ca.crt'}
[textui]
enable=false
[webui]
enable=false
''')
    processes, logs = [], []
    node = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id='CBridge-wire-test')
    messages = queue.Queue()
    node.tls_set_context(client_context(pki / 'ship/ca.crt',
                                       pki / 'issuer/bridge/client.crt', pki / 'issuer/bridge/client.key'))
    response_topic = '/oneM2M/resp/CBridge/id-ship/json'
    notification_topic = '/oneM2M/req/id-ship/CBridge/json'
    def on_message(client, userdata, message):
        body = json.loads(message.payload)
        if message.topic == notification_topic:
            client.publish('/oneM2M/resp/id-ship/CBridge/json', json.dumps({
                'rsc': 2000, 'rqi': body['rqi'], 'rvi': '3', 'fr': 'CBridge', 'to': '/id-ship'}))
        messages.put((message.topic, body))
    node.on_message = on_message
    node.on_connect = lambda client, userdata, flags, reason, properties: client.subscribe([
        (response_topic, 1), (notification_topic, 1)])

    def wait_message(predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                topic, body = messages.get(timeout=.2)
            except queue.Empty:
                continue
            if predicate(topic, body):
                return body
        pytest.fail('MQTTS response/notification timeout')

    def request(rqi, target, ty, body):
        node.publish('/oneM2M/req/CBridge/id-ship/json', json.dumps({
            'op': 1, 'fr': 'CBridge', 'to': target, 'rqi': rqi, 'rvi': '3', 'ty': ty, 'pc': body}))
        return wait_message(lambda topic, value: topic == response_topic and value.get('rqi') == rqi)

    try:
        for command, cwd in [([broker_binary, '-c', str(broker_config)], tmp_path),
                             ([sys.executable, '-m', 'anchor.acme_tls', '--headless',
                               '--base-directory', str(acme_dir), '--config', 'acme.ini'], acme_dir)]:
            log = (tmp_path / f'process-{len(processes)}.log').open('w'); logs.append(log)
            processes.append(subprocess.Popen(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                               env={**os.environ, 'ANCHOR_TLS_DIR': str(pki / 'ship')}))
            if len(processes) == 1:
                deadline = time.monotonic() + 5
                while True:
                    try:
                        node.connect('127.0.0.1', mqtt_port)
                        node.loop_start()
                        break
                    except OSError:
                        if time.monotonic() > deadline:
                            raise
                        time.sleep(.1)
        admin = Client(Endpoint(f'https://127.0.0.1:{https_port}', 'cse-ship', '/id-ship',
                                ca=str(pki / 'ship/ca.crt')), 'CAdmin')
        deadline = time.monotonic() + 25
        while True:
            try:
                admin.get('cse-ship')
                break
            except Exception:
                if time.monotonic() > deadline or processes[-1].poll() is not None:
                    pytest.fail((tmp_path / 'process-1.log').read_text()[-4000:])
                time.sleep(.1)
        # Creation response proves the ACME MQTT binding received and processed uplink.
        assert request('ae', 'cse-ship', 2, {'m2m:ae': {'rn': 'bridge', 'api': 'Nanchor.bridge',
            'rr': True, 'srv': ['3'], 'poa': [f'mqtts://127.0.0.1:{mqtt_port}']}})['rsc'] == 2001
        assert request('cnt', 'cse-ship/bridge', 3, {'m2m:cnt': {'rn': 'command'}})['rsc'] == 2001
        assert request('sub', 'cse-ship/bridge/command', 23, {'m2m:sub': {'rn': 'sub-node',
            'nu': ['CBridge'], 'enc': {'net': [3]}, 'nct': 1}})['rsc'] == 2001
        # Write command over verified HTTPS; the device receives it over MQTTS.
        admin.create('cse-ship/bridge/command', 4, {'m2m:cin': {'con': 'tls-command'}})
        notification = wait_message(lambda topic, body: topic == notification_topic and
                                    'nev' in body.get('pc', {}).get('m2m:sgn', {}))
        assert notification['pc']['m2m:sgn']['nev']['rep']['m2m:cin']['con'] == 'tls-command'
    finally:
        node.disconnect(); node.loop_stop()
        for process in reversed(processes):
            process.terminate()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
        for log in logs:
            log.close()
