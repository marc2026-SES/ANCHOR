"""Offline DDS contract, QoS and permission checks; no DDS participant is created."""
import queue
import shutil
import subprocess
from pathlib import Path

import pytest

from anchor.config import Endpoint, Settings
from anchor.dds.config import Config, QoS
from anchor.dds.model import Window, validate_batch
from anchor.dds.resources import ORIGIN, provision
from anchor.dds.runtime import Counters, offer_latest
from anchor.resources import bootstrap
from fake_cse import MemoryCSE


def batch(seq=0, boot='0123456789abcdef'):
    return {'schemaVersion': 1, 'vesselId': 'ship-001', 'deviceId': 'engine',
            'bootId': boot, 'seq': seq, 'startedAtMs': 100000,
            'offsetsUs': [0, 5000], 'xyzG': [[0.0, 0.0, 1.0], [1.0, 0.0, 1.0]]}


@pytest.mark.parametrize('change', [
    {'deviceId':'bridge'}, {'vesselId':'spoof'}, {'seq':True}, {'bootId':'../../secret'}, {'bootId':1234567890123456},
    {'startedAtMs':0}, {'startedAtMs':110000}, {'offsetsUs':[0,0]},
    {'xyzG':[[float('nan'),0,0],[0,0,1]]}, {'xyzG':[[0,0,100],[0,0,1]]},
    {'xyzG':[]}, {'extra':'field'}])
def test_rejects_bad_or_spoofed_vibration_batch(change):
    with pytest.raises(ValueError):
        validate_batch({**batch(),**change},'ship-001',100100,1500)


def test_lifespan_does_not_slide_and_preserves_sample_offsets():
    value=batch()
    assert validate_batch(value,'ship-001',101499,1500) is value
    with pytest.raises(ValueError):validate_batch(value,'ship-001',101500,1500)
    value['offsetsUs']=[0,1000000]
    with pytest.raises(ValueError):validate_batch(value,'ship-001',100100,1500)


def test_windows_keep_raw_samples_without_mixing_durable_identity():
    window=Window('ship-001',1000)
    assert window.add(batch())
    assert not window.add(batch())
    assert window.add(batch(2))
    assert window.add(batch(0,'abcdef0123456789'))
    result=window.flush(101000,QoS().describe())
    assert result['sampleCount']==6
    assert result['batches'][0]==batch()
    assert result['rmsMagnitudeACG'] > 0
    assert window.counts['observedBatchGaps']==1
    assert window.counts['duplicates']==1
    assert window.flush(102000,{}) is None


def test_window_and_transfer_queues_have_finite_limits():
    window=Window('ship-001',1000)
    for seq in range(250):window.add(batch(seq))
    assert window.samples==400 and window.counts['capacityDrops']==100
    pending=queue.Queue(2);counts=Counters()
    for i in range(5):offer_latest(pending,i,counts,'drops')
    assert [pending.get_nowait(),pending.get_nowait()]==[3,4]
    assert counts.snapshot()=={'drops':3}


@pytest.mark.parametrize('change', [{'depth':0}, {'depth':True}, {'history':'UNBOUNDED'},
                                  {'deadline_ms':-1}, {'lifespan_ms':0}, {'reliability':'TCP'},
                                  {'durability':'PERSISTENT'}, {'lease_ms':0}])
def test_invalid_qos_is_rejected(change):
    with pytest.raises(ValueError):QoS(**change)


def test_actual_native_qos_and_cdr_without_ros_or_network():
    pytest.importorskip('cyclonedds')
    from cyclonedds.qos import Policy
    from anchor.dds.wire import VibrationBatch
    profile=QoS(reliability='RELIABLE',durability='TRANSIENT_LOCAL',history='KEEP_ALL',
                liveliness='MANUAL_BY_TOPIC')
    writer,reader=profile.policies(True),profile.policies(False)
    assert writer[Policy.Reliability]==Policy.Reliability.Reliable(20000000)
    assert writer[Policy.Durability]==Policy.Durability.TransientLocal
    assert writer[Policy.History]==Policy.History.KeepAll
    assert writer[Policy.Deadline].deadline==300000000
    assert writer[Policy.Lifespan].lifespan==1500000000
    assert reader[Policy.Lifespan] is None
    assert writer[Policy.Liveliness]==Policy.Liveliness.ManualByTopic(2000000000)
    encoded=VibrationBatch.from_json(batch()).serialize()
    assert VibrationBatch.deserialize(encoded).to_json()==batch()


def test_ipe_bootstrap_and_acp_do_not_grant_control_permissions():
    from test_access_control import permission
    endpoint=Endpoint('https://edge','cse-edge','/id-edge')
    cfg=Settings('edge',{'edge':endpoint},dds_ipe_enabled=True)
    server=MemoryCSE(endpoint)
    bootstrap(cfg,server.client('CAnchorEdge'))
    provision(server.client())
    assert permission(server,'ddsIpe/vibration',ORIGIN,1,4)
    assert not permission(server,'ddsIpe/vibration',ORIGIN,1,23)
    assert not permission(server,'ddsIpe/vibration',ORIGIN,4)
    assert not permission(server,'ddsIpe/vibration',ORIGIN,8)
    assert permission(server,'ddsIpe/vibration','CAnchorDashboard',2)
    assert not permission(server,'ddsIpe/vibration','CAnchorDashboard',1,4)
    assert not permission(server,'anchor/commands',ORIGIN,1,4)
    assert not permission(server,'anchor/events',ORIGIN,1,4)
    assert not permission(server,'ddsIpe/vibration',ORIGIN,4,self_permission=True)


def test_deployment_modes_and_udp_are_explicit():
    from test_tls_deployment import script
    root=Path(__file__).parents[1]
    for role,mode in [('ship','publisher'),('edge','ipe')]:
        cfg=Config.load(root/'deploy'/role/'dds.json')
        assert cfg.mode==mode
        assert '<Transport>udp</Transport>' in cfg.xml()
        assert '<AllowMulticast>false</AllowMulticast>' in cfg.xml()
        assert cfg.interface=='127.0.0.1'
    with pytest.raises(ValueError):Settings('ship',{'ship':Endpoint('https://ship','cse-ship','/id-ship')},dds_ipe_enabled=True)
    launcher=script('role')
    with pytest.raises(ValueError,match='fixed role config'):
        launcher.main(['ship','dds-check','--config',str(root/'deploy/edge/dds.json')])
    with pytest.raises(ValueError,match='ship publisher and coastal IPE'):
        launcher.main(['shore','dds'])


def test_firmware_raw_batch_limit_and_timer_wrap(tmp_path):
    compiler=shutil.which('c++')
    if not compiler:pytest.skip('Host C++ compiler unavailable')
    source=tmp_path/'raw.cpp'
    source.write_text('''#include <cassert>
#include "RawVibration.h"
int main() {
 AnchorRawVibration b;
 assert(!b.add(0,0,0,0,1));
 assert(b.add(1000,0xfffffff0u,0,0,1));
 assert(b.add(1001,0x10u,1,0,1));
 assert(b.offsetsUs[1]==32);
 assert(!b.due(0x10u)); assert(b.due(100000u));
 for (unsigned i=2;i<20;++i) assert(b.add(1000+i,100+i,0,0,1));
 assert(!b.add(2000,200,0,0,1)); assert(b.due(200));
 b.clear(); assert(b.count==0); assert(!b.due(300));
}
''')
    include=Path(__file__).parents[1]/'hardware/firmware/libraries/AnchorNode'
    binary=tmp_path/'raw'
    subprocess.run([compiler,'-std=c++11','-Wall','-Wextra','-Werror','-I',str(include),str(source),'-o',str(binary)],check=True)
    subprocess.run([str(binary)],check=True)
