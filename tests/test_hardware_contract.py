import copy
import shutil
import subprocess
from pathlib import Path

import pytest

from anchor.commands import make, validate
from anchor.config import Endpoint, Settings
from anchor.events import adapt, digest, fair_batch
from anchor.incidents import tilt_transitions


def event(seq=0, boot='boot-a', roll=30, at=None):
    payload = {'seq': seq, 'bootId': boot, 'ts': (1000 + seq if at is None else at) * 1000,
               'roll': roll, 'pitch': 0}
    return adapt({'ri': 'cin-' + boot + str(seq), 'ct': '20261116T000000'}, payload,
                 'bridge', 'attitude', 'ship-001', '/id-ship')


def settings(**kwargs):
    return Settings('ship', {'ship': Endpoint('http://localhost:8080', 'cse-ship', '/id-ship')}, **kwargs)


def test_raw_hardware_fields_and_units_are_preserved():
    e = event()
    assert e['payload']['roll'] == 30
    assert e['units']['roll'] == 'deg'
    assert 'measurement' not in e
    before = digest(e)
    changed = copy.deepcopy(e)
    changed['payload']['roll'] = 31
    assert digest(changed) != before


def test_boot_identity_separates_restarted_sequences():
    assert event(0, 'first')['eventId'] != event(0, 'second')['eventId']


def test_old_firmware_never_gets_a_guessed_boot_stream():
    e = event()
    payload = dict(e['payload']); payload.pop('bootId')
    a = adapt({'ri': 'old1', 'ct': '20261116T000000'}, payload, 'bridge', 'attitude', 'ship-001', '/id-ship')
    b = adapt({'ri': 'old2', 'ct': '20261116T000000'}, payload, 'bridge', 'attitude', 'ship-001', '/id-ship')
    assert a['quality']['identity'] == 'legacy-resource'
    assert a['eventId'] != b['eventId']


def test_bad_value_and_spoofed_source_are_rejected():
    e = event()
    for payload in ({**e['payload'], 'roll': float('nan')}, {**e['payload'], 'deviceId': 'cargo'}):
        with pytest.raises(ValueError):
            adapt({'ri': 'x', 'ct': '20261116T000000'}, payload, 'bridge', 'attitude', 'ship-001', '/id-ship')


def test_tilt_lifecycle_is_stable_across_replay_and_has_hysteresis():
    rows = [event(i, roll=r) for i, r in enumerate([30, 31, 30, 20, 10, 10, 10])]
    changes = tilt_transitions(rows, settings())
    assert [e['payload']['status'] for e in changes] == ['opened', 'recovered']
    assert changes[0]['payload']['incidentId'] == changes[1]['payload']['incidentId']
    assert tilt_transitions(list(reversed(rows)), settings()) == changes


def test_short_tilt_and_large_sample_gaps_do_not_form_duration_evidence():
    assert not tilt_transitions([event(0), event(1, roll=0), event(2)], settings())
    assert not tilt_transitions([event(0), event(1, at=2000)], settings())


def test_wall_clock_jump_and_invalid_sensor_cannot_fake_tilt_duration():
    rows = [event(i, at=1000 + i*100) for i in range(3)]
    for i, row in enumerate(rows):
        row['payload']['uptimeMs'] = i*100
    assert not tilt_transitions(rows, settings())
    rows = [event(i) for i in range(3)]
    rows[1]['payload']['ok'] = False
    assert not tilt_transitions(rows, settings())


def test_priority_keeps_normal_data_progressing():
    rows = [event(i) for i in range(12)]
    for e in rows[:10]:
        e['priority'] = 'critical'
    selected = fair_batch(rows, 10, 4)
    assert [e['seq'] for e in selected] == [0, 1, 2, 3, 10, 4, 5, 6, 7, 11]


@pytest.mark.parametrize('body', [{'intervalMs': 100}, {'intervalMs': 201}, {'intervalMs': True},
                                  {'durationMs': 0}, {'ttlMs': -1}, {'target': 'fan'}])
def test_device_commands_are_bounded(body):
    with pytest.raises(ValueError):
        make(body, now=1000)


def test_command_deadline_does_not_slide():
    c = make({'commandId': 'fixed', 'ttlMs': 10000, 'durationMs': 2000}, now=1000)
    validate(c)
    assert c['expiresAtMs'] == 11000
    assert c['durationMs'] == 2000


def test_firmware_profile_clock_and_expiration(tmp_path):
    compiler = shutil.which('c++')
    if not compiler:
        pytest.skip('Host C++ compiler unavailable')
    source = tmp_path / 'profile.cpp'
    include = Path(__file__).resolve().parents[1] / 'hardware/firmware/libraries/AnchorNode'
    source.write_text('''#include <cassert>
#include "ReportProfile.h"
int main() {
  AnchorReportProfile p;
  assert(p.current(100) == 1000);
  assert(!p.valid(199, 1000)); assert(!p.valid(201, 1000));
  assert(!p.valid(200, 300001)); assert(p.valid(200, 30000));
  p.intervalMs = 200; p.untilMs = 5000;
  assert(p.current(0) == 1000); assert(p.current(4999) == 200);
  assert(p.current(5000) == 1000); assert(p.current(9000) == 1000);
}
''')
    binary = tmp_path / 'profile'
    subprocess.run([compiler, '-std=c++11', '-Wall', '-Wextra', '-Werror', '-I', str(include), str(source), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
