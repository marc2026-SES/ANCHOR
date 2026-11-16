"""Deterministic tilt lifecycle replayed from CSE records after a restart."""
from .events import record_name


def tilt_transitions(events, settings):
    rule = settings.tilt
    streams = {}
    for e in events:
        if e['kind'] == 'measurement' and e['sensor'] == 'attitude' and e['quality']['identity'] != 'legacy-resource':
            streams.setdefault((e['deviceId'], e['bootId']), []).append(e)
    result = []
    for _, rows in sorted(streams.items()):
        candidate = recovery = active = previous = None
        for e in sorted(rows, key=lambda v: (v['seq'], v['eventId'])):
            # MCU uptime is duration evidence even before wall-clock sync.
            t = e['payload'].get('uptimeMs', e['generatedAt'] * 1000) / 1000
            if e['payload'].get('ok') is False:
                candidate = recovery = None
                previous = None
                continue
            if previous is not None and (t < previous or t - previous > rule['maxGapSeconds']):
                candidate = recovery = None
            previous = t
            angle = max(abs(e['payload']['roll']), abs(e['payload']['pitch']))
            if active is None:
                if angle < rule['trigger']:
                    candidate = None
                    continue
                candidate = candidate or (e, t)
                if t - candidate[1] < rule['holdSeconds']:
                    continue
                active = 'tilt-' + record_name(candidate[0]['eventId'])
                result.append(_transition(e, active, 'opened', candidate[0]['eventId'], settings))
            elif angle <= rule['recover']:
                recovery = recovery or (e, t)
                if t - recovery[1] >= rule['recoverSeconds']:
                    result.append(_transition(e, active, 'recovered', recovery[0]['eventId'], settings))
                    active = candidate = recovery = None
            else:
                recovery = None
    return result


def _transition(e, incident_id, status, since, settings):
    return {'schemaVersion': 2, 'eventId': incident_id + ':' + status, 'vesselId': e['vesselId'],
            'deviceId': 'ship-rule', 'bootId': e['bootId'], 'seq': e['seq'],
            'generatedAt': e['generatedAt'], 'kind': 'incident', 'sensor': 'tilt', 'priority': 'critical',
            'payload': {'incidentId': incident_id, 'status': status, 'rule': 'tilt-duration-v1',
                        'thresholds': settings.tilt, 'evidenceEventId': e['eventId'], 'sinceEventId': since,
                        'roll': e['payload']['roll'], 'pitch': e['payload']['pitch']}}
