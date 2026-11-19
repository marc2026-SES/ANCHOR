"""Latest ship measurements projected into the CSE's standard SDT flexContainers."""
from dataclasses import dataclass
import json
import logging
import math
import threading

from .client import resource
from .resources import assign_policy


@dataclass(frozen=True)
class Model:
    sensor: str
    path: str
    tag: str
    module: str
    fields: tuple
    label: str
    unit: str

    @property
    def cnd(self):
        return 'org.onem2m.common.moduleclass.' + self.module

    def labels(self, quality):
        return ['anchor:sdt', self.label, 'unit:' + self.unit, 'quality:' + quality]


GPS = Model('position', 'bridge/shipLocation', 'cod:geoLn', 'geoLocation',
            (('lat', 'latie'), ('lon', 'longe')), 'ship:gps', 'degree')
VIBRATION = Model('vibration', 'engine/vibrationRms', 'cod:numVe', 'numberValue',
                  (('vib_rms_g', 'numVe'),), 'engine:vibration-rms', 'g')
TEMPERATURE = Model('climate', 'cargo/temperature', 'cod:tempe', 'temperature',
                    (('temp_c', 'curT0'),), 'cargo:temperature', 'degC')
MODELS = {(m.path.split('/')[0], m.sensor): m for m in (GPS, VIBRATION, TEMPERATURE)}


def provision(admin, model, policy):
    parent, name = model.path.split('/')
    attrs = {short: 0.0 for _, short in model.fields}
    attrs.update(rn=name, cnd=model.cnd, lbl=model.labels('unobserved'), acpi=[policy])
    row = resource(admin.ensure(admin.root + '/' + parent, 28, {model.tag: attrs}))
    if row.get('cnd') != model.cnd:
        raise ValueError(f'{model.path} has a different flexContainer specialization')
    assign_policy(admin, admin.root + '/' + model.path, model.tag, policy, 'CAdmin', row)


def sample_values(model, payload):
    values = {short: payload.get(key) for key, short in model.fields}
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in values.values()):
        return None
    if model == GPS and (payload.get('fix') is not True or payload.get('gps') != 'ok'
                         or abs(values['latie']) > 90 or abs(values['longe']) > 180):
        return None
    if model == VIBRATION and values['numVe'] < 0:
        return None
    if model == TEMPERATURE and payload.get('ok') is not True:
        return None
    return values


class LatestSDT:
    """One pending value per model. Only the background worker performs CSE I/O."""
    def __init__(self, client):
        self.client = client
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.latest = {}
        self.applied = {}

    def offer(self, event):
        model = MODELS.get((event['deviceId'], event['sensor']))
        if model is None:
            return
        values = sample_values(model, event['payload'])
        stamp = event['source']['ct']
        if values is None or not isinstance(stamp, str) or not stamp:
            return
        with self.lock:
            if model in self.latest and stamp <= self.latest[model][0]:
                return
            self.latest[model] = (stamp, values)
            self.wake.set()

    def publish(self, model, stamp, values):
        path = self.client.root + '/' + model.path
        if model not in self.applied:
            row = self.client.get(path)[model.tag]
            if row.get('cnd') != model.cnd:
                raise ValueError(f'Unexpected SDT specialization at {path}')
            observed = 'quality:observed' in row.get('lbl', ())
            previous = {k: row.get(k) for k in values} if observed else None
            self.applied[model] = (row.get('dgt', ''), previous)
        previous_stamp, previous = self.applied[model]
        if stamp <= previous_stamp:
            return
        if previous != values:
            self.client.update(path, {model.tag: {**values, 'dgt': stamp, 'lbl': model.labels('observed')}})
        self.applied[model] = (stamp, values)

    def run(self, stop):
        while not stop.is_set():
            self.wake.wait()
            with self.lock:
                self.wake.clear()
                latest = self.latest.copy()
            retry = False
            for model, (stamp, values) in latest.items():
                if stop.is_set():
                    return
                try:
                    self.publish(model, stamp, values)
                except Exception as exc:
                    logging.warning('SDT update failed for %s: %s', model.path, exc)
                    self.applied.pop(model, None)
                    retry = True
            if retry:
                stop.wait(2)
                self.wake.set()


def read_observed(client):
    """Discover observed flexContainers, then read authorized measurements by URI."""
    result = {}
    for model in MODELS.values():
        parent = client.root + '/' + model.path.split('/')[0]
        refs = client.get(parent, params={'fu': 1, 'drt': 1, 'ty': 28, 'lvl': 1,
                                          'lbl': 'quality:observed'})['m2m:uril']
        for ref in refs:
            row = client.get(ref).get(model.tag, {})
            labels = row.get('lbl', ())
            if row.get('cnd') != model.cnd or 'quality:observed' not in labels:
                continue
            if model == VIBRATION and model.label not in labels:
                continue
            path = ref.split(client.root + '/', 1)[-1]
            result[path] = dict(values={k: row[k] for _, k in model.fields},
                                unit=model.unit, sampleTime=row.get('dgt'))
    return result


if __name__ == '__main__':
    import argparse
    from .client import Client
    from .config import ORIGINS, Settings

    parser = argparse.ArgumentParser(description='Discover and read observed ship SDT resources')
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    settings = Settings.load(args.config)
    if settings.role != 'ship':
        parser.error('Use the ship role configuration')
    print(json.dumps(read_observed(Client(settings.nodes['ship'], ORIGINS['ship'])), indent=2))
