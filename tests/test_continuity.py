import copy
import time

import pytest

from anchor.commands import dispatch, make
from anchor.config import Endpoint, ORIGINS, Settings
from anchor.events import digest, record_name
from anchor.runtime import Runtime
from anchor.store import CapacityError, Journal
from fake_cse import MemoryCSE


@pytest.fixture
def fleet():
    nodes = {role: Endpoint('http://' + role, 'cse-' + role, '/id-' + role) for role in ('ship', 'edge', 'shore')}
    servers = {role: MemoryCSE(endpoint) for role, endpoint in nodes.items()}
    def build(role):
        return Runtime(Settings(role, nodes), {p: s.client(ORIGINS[role]) for p, s in servers.items()})
    runtimes = {role: build(role) for role in nodes}
    for runtime in runtimes.values():
        runtime.provision()
    return runtimes, servers, build


def emit(fleet, seq=0, boot='boot1', device='bridge', sensor='attitude', **fields):
    servers = fleet[1]
    payload = {'seq': seq, 'bootId': boot, 'ts': (1794787200 + seq)*1000, 'roll': 30, 'pitch': 0, **fields}
    servers['ship'].client().put_record('cse-ship/' + device + '/' + sensor, record_name(boot + str(seq)), payload)


def source(fleet, count=3):
    for i in range(count):
        emit(fleet, i)
    fleet[0]['ship'].local_tick()


def test_coast_survives_nonoverlapping_windows_and_full_app_restart(fleet):
    apps, servers, build = fleet
    ship, edge, shore = (apps[r] for r in ('ship', 'edge', 'shore'))
    servers['shore'].online = False
    source(fleet)
    snapshot = ship.capture_batch('movie')
    assert len(snapshot['expected']) == 4  # 3 source records + 1 incident
    ship.transfer_tick('edge'); edge.local_tick(); ship.transfer_tick('edge'); ship.local_tick()
    assert ship.status()['delivery']['coastCustody'] == 4
    assert ship.status()['delivery']['shoreConfirmed'] == 0
    servers['ship'].online = False
    servers['shore'].online = True
    edge = build('edge'); edge.local_tick(); edge.transfer_tick('shore')
    shore.local_tick(); edge.transfer_tick('shore')
    assert shore.status()['delivery']['shoreConfirmed'] == 4
    servers['ship'].online = True
    ship = build('ship'); ship.local_tick(); ship.transfer_tick('edge'); ship.local_tick()
    assert ship.batch_status('movie')['complete']
    assert len(ship._events) == 4


def test_lost_ack_replays_same_body_without_duplicate(fleet):
    apps, servers, _ = fleet
    source(fleet, 1)
    ship, edge = apps['ship'], apps['edge']
    original = ship.clients['edge'].put_record
    calls = []
    def lost_ack(*args):
        result = original(*args)
        calls.append(args)
        if len(calls) == 1:
            raise TimeoutError('ACK lost after commit')
        return result
    ship.clients['edge'].put_record = lost_ack
    with pytest.raises(TimeoutError):
        ship.transfer_tick('edge')
    ship.transfer_tick('edge'); edge.local_tick()
    assert len(edge._events) == 1
    assert edge._events[0] == ship._events[0]


def test_conflict_quarantine_does_not_stop_following_event(fleet):
    apps, _, _ = fleet
    source(fleet, 2)
    bad = copy.deepcopy(apps['ship']._events[0]); bad['payload']['roll'] = 999
    apps['edge'].journal.put('events', bad['eventId'], bad)
    apps['ship'].transfer_tick('edge'); apps['edge'].local_tick()
    assert len(apps['edge']._events) == 2
    assert list(apps['ship'].journal.values('quarantine'))


def test_malformed_sensor_does_not_block_valid_sensor_or_local_rule(fleet):
    apps, servers, _ = fleet
    servers['ship'].client().create('cse-ship/bridge/attitude', 4, {'m2m:cin': {'con': '[broken'}})
    source(fleet)
    assert len(apps['ship'].status()['incidents']) == 1
    assert list(apps['ship'].journal.values('quarantine'))


def test_capacity_backpressure_does_not_drop_old_data_or_ack_new_data(fleet):
    apps, _, _ = fleet
    source(fleet, 2)
    ship, edge = apps['ship'], apps['edge']
    ship.settings.max_records = 1
    with pytest.raises(CapacityError):
        ship.transfer_tick('edge')
    edge.local_tick(); ship.local_tick()
    assert len(edge._events) == 1
    assert ship.status()['delivery']['shoreConfirmed'] == 0
    assert len(ship._events) == 2


def test_stale_custody_does_not_hide_deleted_peer_data(fleet):
    apps, servers, _ = fleet
    source(fleet, 1)
    ship, edge = apps['ship'], apps['edge']
    ship.transfer_tick('edge'); edge.local_tick()
    path = 'cse-edge/anchor/events/' + record_name(ship._events[0]['eventId'])
    del servers['edge'].rows[path]
    ship.transfer_tick('edge')
    assert path in servers['edge'].rows


def test_command_cached_before_outage_and_no_simulated_success(fleet):
    apps, servers, _ = fleet
    ship, edge, shore = (apps[r] for r in ('ship', 'edge', 'shore'))
    command = shore.submit_command({'commandId': 'cmd', 'ttlMs': 120000})
    edge.cache_commands_tick('shore'); edge.local_tick()
    servers['shore'].online = False
    ship.cache_commands_tick('edge'); ship.local_tick(); ship.command_tick()
    assert ship.status()['commands'][0]['status'] == 'dispatched'
    assert ship.status()['commands'][0]['devices'] == {}
    for device in ('bridge', 'engine'):
        emit(fleet, boot='result-' + device, device=device, sensor='commandResults', commandId='cmd',
             command=command, status='applied', driver='esp32', appliedIntervalMs=200,
             appliedAtMs=command['issuedAtMs'], profileUntilMs=command['issuedAtMs'] + command['durationMs'])
    ship.local_tick()
    assert ship.status()['commands'][0]['status'] == 'applied'
    ship.transfer_tick('edge'); edge.local_tick()
    servers['ship'].online = False; servers['shore'].online = True
    edge.transfer_tick('shore'); shore.local_tick()
    assert shore.status()['commands'][0]['status'] == 'applied'


def test_expired_command_is_not_fanned_out(fleet):
    ship = fleet[0]['ship']
    command = make({'commandId': 'expired'}, now=1)
    assert dispatch(ship.local, ship.journal, command, 100000, {}) == 'expired'
    assert not ship.local.cins('cse-ship/bridge/command')
    assert not ship.local.cins('cse-ship/engine/command')


def test_mismatched_device_report_cannot_claim_applied(fleet):
    apps, _, _ = fleet
    command = apps['shore'].submit_command({'commandId': 'bad-result'})
    apps['ship'].journal.put('commands', command['commandId'], command)
    for device in ('bridge', 'engine'):
        emit(fleet, boot='bad-' + device, device=device, sensor='commandResults', commandId=command['commandId'],
             command=command, status='applied', driver='esp32', appliedIntervalMs=1000,
             appliedAtMs=command['issuedAtMs'], profileUntilMs=command['issuedAtMs'] + command['durationMs'])
    apps['ship'].local_tick()
    assert apps['ship'].status()['commands'][0]['status'] != 'applied'


def test_frozen_batch_denominator_survives_new_traffic(fleet):
    apps, _, _ = fleet
    source(fleet, 1)
    ship, edge, shore = (apps[r] for r in ('ship', 'edge', 'shore'))
    ship.capture_batch('bounded')
    ship.transfer_tick('edge'); edge.local_tick(); edge.transfer_tick('shore'); shore.local_tick()
    edge.transfer_tick('shore'); ship.transfer_tick('edge')
    emit(fleet, 0, boot='later'); ship.local_tick()
    assert ship.batch_status('bounded') == {'batchId': 'bounded', 'expected': 1, 'pending': [], 'complete': True}
    assert ship.status()['delivery']['pendingFinal'] == 1


def test_edge_operator_ack_is_durable_and_goes_to_shore(fleet):
    apps, _, _ = fleet
    source(fleet)
    ship, edge, shore = (apps[r] for r in ('ship', 'edge', 'shore'))
    ship.transfer_tick('edge'); edge.local_tick()
    incident_id = edge.status()['incidents'][0]['payload']['incidentId']
    ack = edge.acknowledge(incident_id, '연안 담당자')
    assert edge.acknowledge(incident_id, '연안 담당자') == ack
    edge.local_tick(); edge.transfer_tick('shore'); shore.local_tick()
    assert any(e['kind'] == 'incidentAck' for e in shore.status()['incidents'])


def test_boot_sequence_gaps_are_not_filled_by_other_boot(fleet):
    apps, _, _ = fleet
    emit(fleet, 0); emit(fleet, 2)
    emit(fleet, 0, boot='new'); emit(fleet, 1, boot='new'); emit(fleet, 2, boot='new')
    apps['ship'].local_tick()
    assert apps['ship'].status()['sequenceGaps'] == {'bridge:boot1': 1, 'bridge:new': 0}


def test_bad_forwarded_envelope_cannot_block_receiver_receipts(fleet):
    apps, _, _ = fleet
    source(fleet, 1)
    bad = {'schemaVersion': 2, 'eventId': 'malformed', 'deviceId': 'bridge', 'bootId': 'b',
           'vesselId': 'ship-001', 'generatedAt': 1, 'kind': 'measurement', 'priority': 'normal', 'payload': {}}
    apps['edge'].journal.put('events', 'malformed', bad)
    apps['ship'].transfer_tick('edge'); apps['edge'].local_tick()
    assert apps['edge'].status()['delivery']['coastCustody'] == 1
    assert len(list(apps['edge'].journal.values('quarantine'))) == 1


def test_valid_climate_updates_standard_temperature_without_fake_data(fleet):
    apps, servers, _ = fleet
    emit(fleet, 0, boot='climate', device='cargo', sensor='climate', ok=False)
    apps['ship'].local_tick()
    assert servers['ship'].rows['cse-ship/cargo/temperature']['lbl'] == ['anchor:sdt', 'quality:unobserved']
    emit(fleet, 1, boot='climate', device='cargo', sensor='climate', ok=True, temp_c=24.5, rh=50)
    apps['ship'].local_tick()
    assert servers['ship'].rows['cse-ship/cargo/temperature']['curT0'] == 24.5


def test_repeat_bootstrap_keeps_sensor_records(fleet):
    source(fleet)
    ship = fleet[0]['ship']
    before = copy.deepcopy(ship._events)
    ship.provision(); ship.local_tick()
    assert ship._events == before


def test_local_archive_loss_is_reconstructed_from_retained_raw_history(fleet):
    source(fleet, 1)
    apps, servers, _ = fleet
    ship = apps['ship']
    event = copy.deepcopy(ship._events[0])
    path = 'cse-ship/anchor/events/' + record_name(event['eventId'])
    del servers['ship'].rows[path]
    ship.local_tick()  # deliberately keep the process and its optimization cache alive
    assert ship._events == [event]
