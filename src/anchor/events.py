"""Hardware CINs stay intact inside a versioned, immutable transport envelope."""
from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone

from .client import canonical
from .config import SENSORS

UNITS = {'attitude': {'roll': 'deg', 'pitch': 'deg', 'gx': 'deg/s', 'gy': 'deg/s', 'gz': 'deg/s',
                       'ax': 'g', 'ay': 'g', 'az': 'g'},
         'position': {'lat': 'deg', 'lon': 'deg', 'sog_kn': 'kn', 'cog': 'deg'},
         'vibration': {'vib_rms_g': 'g', 'vib_peak_g': 'g', 'mean_g': 'g'},
         'gas': {'mq2_v': 'V'}, 'climate': {'temp_c': 'degC', 'rh': '%'}, 'hatch': {'open_s': 's'}}


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def record_name(identity):
    return 'e-' + hashlib.sha256(identity.encode()).hexdigest()[:32]


def ct_time(value):
    base, _, fraction = value.partition(',')
    return datetime.strptime(base, '%Y%m%dT%H%M%S').replace(tzinfo=timezone.utc).timestamp() + (float('0.' + fraction) if fraction else 0)


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def adapt(cin, payload, device, sensor, vessel_id, csi):
    if device not in SENSORS or sensor not in (*SENSORS[device], 'commandResults'):
        raise ValueError('Unsupported hardware resource')
    if not isinstance(payload, dict):
        raise ValueError('Hardware con must be an object')
    if payload.get('deviceId', device) != device or payload.get('vesselId', vessel_id) != vessel_id:
        raise ValueError('Hardware identity does not match its source')
    if sensor == 'attitude' and not all(number(payload.get(k)) for k in ('roll', 'pitch')):
        raise ValueError('Invalid attitude')
    seq = payload.get('seq')
    if type(seq) is not int or seq < 0:
        raise ValueError('Invalid hardware sequence')
    boot = payload.get('bootId')
    legacy = not boot
    if legacy:
        # Old firmware has no reboot identity. Never infer loss-free streams from its seq.
        boot = 'legacy-' + digest([csi, cin['ri'], cin['ct']])[:24]
    if not isinstance(boot, str) or ':' in boot:
        raise ValueError('Invalid bootId')
    event_id = f'{vessel_id}:{device}:{boot}:{seq}'
    if payload.get('eventId', event_id) != event_id:
        raise ValueError('Invalid eventId')
    ts = payload.get('ts', 0)
    generated = ts / 1000 if number(ts) and ts > 0 else ct_time(cin['ct'])
    quality = {'identity': 'legacy-resource' if legacy else 'device-boot-sequence',
               'time': 'device' if number(ts) and ts > 0 else 'cse',
               'droppedBefore': payload.get('droppedBefore', 0)}
    if sensor == 'position':
        quality['validFix'] = payload.get('fix') is True and payload.get('gps') == 'ok'
    elif sensor == 'climate':
        quality['sensorOk'] = payload.get('ok') is True
    elif sensor == 'gas':
        quality.update(warmed=payload.get('warm') is True, calibratedConcentration=False)
    return {'schemaVersion': 2, 'eventId': event_id, 'vesselId': vessel_id, 'deviceId': device,
            'bootId': boot, 'seq': seq, 'generatedAt': generated,
            'kind': 'commandResult' if sensor == 'commandResults' else 'measurement',
            'sensor': sensor, 'priority': 'critical' if sensor == 'commandResults' else 'normal',
            'units': UNITS.get(sensor, {}), 'quality': quality,
            'source': {'csi': csi, 'ri': cin['ri'], 'ct': cin['ct']}, 'payload': payload}


def validate_event(value):
    if not isinstance(value, dict) or value.get('schemaVersion') != 2:
        raise ValueError('Invalid envelope version')
    for key in ('eventId', 'vesselId', 'deviceId', 'bootId'):
        if not isinstance(value.get(key), str) or not value[key]:
            raise ValueError(f'Invalid {key}')
    if value.get('kind') not in ('measurement', 'incident', 'commandResult', 'incidentAck'):
        raise ValueError('Invalid event kind')
    if not number(value.get('generatedAt')) or value.get('priority') not in ('critical', 'normal'):
        raise ValueError('Invalid event time/priority')
    if not isinstance(value.get('payload'), dict):
        raise ValueError('Invalid event payload')
    kind, payload = value['kind'], value['payload']
    if kind in ('measurement', 'commandResult'):
        device, sensor = value['deviceId'], value.get('sensor')
        if device not in SENSORS or sensor not in (*SENSORS[device], 'commandResults'):
            raise ValueError('Invalid hardware stream')
        if (kind == 'commandResult') != (sensor == 'commandResults'):
            raise ValueError('Sensor does not match event kind')
        if type(value.get('seq')) is not int or value['seq'] < 0:
            raise ValueError('Invalid sequence')
        if value['eventId'] != ':'.join(str(value[k]) for k in ('vesselId', 'deviceId', 'bootId', 'seq')):
            raise ValueError('Envelope identity mismatch')
        if not isinstance(value.get('quality'), dict) or value['quality'].get('identity') not in ('legacy-resource', 'device-boot-sequence'):
            raise ValueError('Missing identity quality')
        if sensor == 'attitude' and not all(number(payload.get(k)) for k in ('roll', 'pitch')):
            raise ValueError('Invalid attitude values')
        if 'uptimeMs' in payload and (not number(payload['uptimeMs']) or payload['uptimeMs'] < 0):
            raise ValueError('Invalid uptime')
    elif kind == 'incident':
        if not isinstance(payload.get('incidentId'), str) or payload.get('status') not in ('opened', 'recovered'):
            raise ValueError('Invalid incident lifecycle')
    elif not isinstance(payload.get('incidentId'), str) or not isinstance(payload.get('operator'), str):
        raise ValueError('Invalid incident acknowledgement')
    canonical(value)


def fair_batch(events, limit, critical_burst=4):
    """Persistent caller rotates by receipts: 4 critical, then 1 ordinary by default."""
    key = lambda e: (e['generatedAt'], e['eventId'])
    urgent = sorted((e for e in events if e['priority'] == 'critical'), key=key)
    normal = sorted((e for e in events if e['priority'] != 'critical'), key=key)
    result = []
    ui = ni = 0
    while len(result) < limit and (ui < len(urgent) or ni < len(normal)):
        for _ in range(critical_burst):
            if ui < len(urgent) and len(result) < limit:
                result.append(urgent[ui]); ui += 1
        if ni < len(normal) and len(result) < limit:
            result.append(normal[ni]); ni += 1
    return result
