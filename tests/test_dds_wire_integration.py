"""Opt-in native DDS across two processes; this does not simulate ESP32 hardware.

ANCHOR_DDS_WIRE_INTEGRATION=1 python -m pytest tests/test_dds_wire_integration.py -v
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get('ANCHOR_DDS_WIRE_INTEGRATION') != '1', reason='opt-in native DDS UDP exchange')]


def test_separate_processes_exchange_real_dds_vibration(tmp_path):
    from anchor.dds.config import Config, QoS
    from anchor.dds.wire import VibrationBatch
    from cyclonedds.domain import Domain, DomainParticipant
    from cyclonedds.pub import DataWriter
    from cyclonedds.topic import Topic
    cfg=Config('publisher','',domain_id=93,qos=QoS(reliability='RELIABLE'))
    domain=Domain(cfg.domain_id,cfg.xml())
    participant=DomainParticipant(cfg.domain_id)
    topic=Topic(participant,cfg.topic,VibrationBatch)
    writer=DataWriter(participant,topic,qos=cfg.qos.policies(True))
    receiver=tmp_path/'receive.py'
    receiver.write_text('''import json,time
from anchor.dds.config import Config,QoS
from anchor.dds.wire import VibrationBatch
from cyclonedds.domain import Domain,DomainParticipant
from cyclonedds.sub import DataReader
from cyclonedds.topic import Topic
cfg=Config('ipe','',domain_id=93,qos=QoS(reliability='RELIABLE'))
domain=Domain(cfg.domain_id,cfg.xml())
participant=DomainParticipant(cfg.domain_id)
topic=Topic(participant,cfg.topic,VibrationBatch)
reader=DataReader(participant,topic,qos=cfg.qos.policies(False))
until=time.monotonic()+10
while time.monotonic()<until:
    for value in reader.take(10):
        if hasattr(value,'to_json'):
            print(json.dumps(value.to_json()),flush=True)
            raise SystemExit(0)
    time.sleep(.01)
raise SystemExit('No DDS sample received')
''')
    env=dict(os.environ)
    env['PYTHONPATH']=str(Path(__file__).parents[1]/'src')
    proc=subprocess.Popen([sys.executable,str(receiver)],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        until=time.monotonic()+7
        while not writer.get_matched_subscriptions() and time.monotonic()<until:
            if proc.poll() is not None:break
            time.sleep(.02)
        assert writer.get_matched_subscriptions(), 'DDS discovery did not match the second process'
        now=time.time_ns()//1000000
        value=VibrationBatch('ship-001','0123456789abcdef',7,now,[0,5000],[0.,0.,1.,1.,0.,1.])
        writer.write(value,timestamp=now*1000000)
        stdout,stderr=proc.communicate(timeout=5)
        assert proc.returncode==0,stderr
        assert json.loads(stdout)==value.to_json()
    finally:
        if proc.poll() is None:
            proc.terminate();proc.communicate(timeout=5)
        del writer,topic,participant,domain
