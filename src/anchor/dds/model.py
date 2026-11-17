"""Bounded, timestamped vibration batches; independent of DDS/ROS imports."""
import math
import re
from collections import OrderedDict


def validate_batch(value, vessel_id, now_ms, lifespan_ms):
    if not isinstance(value, dict) or set(value) != {
            'schemaVersion', 'vesselId', 'deviceId', 'bootId', 'seq',
            'startedAtMs', 'offsetsUs', 'xyzG'}:
        raise ValueError('Invalid vibration batch fields')
    if type(value['schemaVersion']) is not int or value['schemaVersion'] != 1 or value['vesselId'] != vessel_id or value['deviceId'] != 'engine':
        raise ValueError('Unexpected vibration source')
    if not isinstance(value['bootId'], str) or not re.fullmatch(r'[0-9a-f]{16}', value['bootId']):
        raise ValueError('Invalid vibration boot identity')
    for key in ('seq', 'startedAtMs'):
        if type(value[key]) is not int or not 0 <= value[key] < 2**63:
            raise ValueError('Invalid vibration ' + key)
    age = now_ms - value['startedAtMs']
    if value['startedAtMs'] == 0 or not -250 <= age < lifespan_ms:
        raise ValueError('Expired or future vibration batch; check clock synchronization')
    offsets, xyz = value['offsetsUs'], value['xyzG']
    if not isinstance(offsets, list) or not 1 <= len(offsets) <= 20 or not isinstance(xyz, list) or len(xyz) != len(offsets):
        raise ValueError('Vibration batch must contain 1..20 samples')
    previous = -1
    for offset, axes in zip(offsets, xyz):
        if type(offset) is not int or not previous < offset <= 1000000:
            raise ValueError('Invalid sample offsets')
        if not isinstance(axes, list) or len(axes) != 3 or any(
                type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 32 for v in axes):
            raise ValueError('Invalid acceleration axes')
        previous = offset
    if offsets[0] != 0 or value['startedAtMs'] + offsets[-1] / 1000 > now_ms + 250:
        raise ValueError('Invalid batch sampling time')
    return value


class Window:
    """Keep at most 400 samples per CSE window; all dropped input is counted."""
    def __init__(self, vessel_id, window_ms):
        self.vessel_id, self.window_ms = vessel_id, window_ms
        self.batches = []
        self.samples = 0
        self.seen = OrderedDict()
        self.last = OrderedDict()
        self.counts = dict(receivedBatches=0, receivedSamples=0, duplicates=0,
                           observedBatchGaps=0, outOfOrder=0, capacityDrops=0)

    def add(self, value):
        key = (value['bootId'], value['seq'])
        if key in self.seen:
            self.counts['duplicates'] += 1
            return False
        self.seen[key] = True
        while len(self.seen) > 512:
            self.seen.popitem(last=False)
        boot, seq = key
        last = self.last.get(boot)
        if last is not None:
            if seq > last + 1:
                self.counts['observedBatchGaps'] += seq - last - 1
            elif seq < last:
                self.counts['outOfOrder'] += 1
        self.last[boot] = max(seq, last if last is not None else seq)
        self.last.move_to_end(boot)
        while len(self.last) > 16:
            self.last.popitem(last=False)
        n = len(value['xyzG'])
        self.counts['receivedBatches'] += 1
        self.counts['receivedSamples'] += n
        if self.samples + n > 400:
            self.counts['capacityDrops'] += n
            return False
        self.batches.append(value)
        self.samples += n
        return True

    def flush(self, now_ms, qos):
        if not self.batches:
            return None
        batches, count = self.batches, self.samples
        self.batches, self.samples = [], 0
        magnitudes = [math.sqrt(sum(x*x for x in axes)) for b in batches for axes in b['xyzG']]
        mean = sum(magnitudes) / count
        rms = math.sqrt(sum((v-mean)**2 for v in magnitudes) / count)
        return {'schemaVersion': 1, 'kind': 'vibrationWindow', 'vesselId': self.vessel_id,
                'deviceId': 'engine', 'unit': 'g', 'receivedAtMs': now_ms,
                'sampleCount': count, 'windowMs': self.window_ms,
                'rmsMagnitudeACG': rms, 'qos': qos, 'batches': batches}
