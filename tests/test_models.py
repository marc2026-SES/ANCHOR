import pytest

from anchor.models import RoutePolicy, make_event, reconcile, validate_event


def test_ack_after_gap_does_not_advance_checkpoint():
    events = [make_event("sensor-01", "boot", n) for n in range(4)]
    result = reconcile(events, [events[0], events[2], events[3]])
    assert result["pending"] == 1
    assert result["checkpoints"]["ship-001:sensor-01:boot"] == 0
    assert not result["complete"]


def test_replayed_duplicates_do_not_hide_missing_records():
    events = [make_event("sensor-01", "boot", n) for n in range(2)]
    result = reconcile(events, [events[0], events[0]])
    assert result["shoreUnique"] == 1
    assert result["pending"] == 1


def test_same_identity_different_body_is_corruption():
    event = make_event("sensor-01", "boot", 0)
    corrupt = {**event, "measurement": {"curT0": 999}}
    result = reconcile([event], [corrupt])
    assert result["conflictingEventIds"] == [event["eventId"]]
    assert not result["complete"]


def test_device_reboot_has_a_new_stream():
    a, b = make_event("sensor-01", "a", 0), make_event("sensor-01", "b", 0)
    assert a["eventId"] != b["eventId"]
    assert reconcile([a, b], [a, b])["complete"]


def test_malformed_device_identity_is_rejected():
    event = make_event("sensor-01", "boot", 0)
    event["seq"] = 1
    with pytest.raises(ValueError):
        validate_event(event)


def test_stale_or_future_location_never_keeps_a_route_active():
    policy = RoutePolicy()
    link = {"zone": "coastal", "coastalAvailable": True, "rssi": -60, "observedAt": 10}
    assert policy.choose(link, "coastal", 30) is None
    assert policy.choose(link, "coastal", 0) is None


def test_hysteresis_prevents_boundary_flapping():
    policy = RoutePolicy(hold_seconds=2, dwell_seconds=5)
    link = {"zone": "coastal", "coastalAvailable": True, "satelliteAvailable": True, "rssi": -105, "observedAt": 100}
    assert policy.choose(link, "coastal", 100) == "coastal"
    assert policy.choose(link, "offshore", 100) == "offshore"


def test_new_route_requires_stable_observations():
    policy = RoutePolicy(hold_seconds=2)
    link = {"zone": "coastal", "coastalAvailable": True, "rssi": -70, "observedAt": 100}
    assert policy.choose(link, None, 100) is None
    assert policy.choose(link, None, 103) == "coastal"


def test_outage_is_immediate_despite_minimum_dwell():
    policy = RoutePolicy(last_change=100)
    assert policy.choose({"observedAt": 101}, "coastal", 101) is None
