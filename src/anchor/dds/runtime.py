"""Ship MQTTS -> DDS writer; coastal DDS reader -> bounded oneM2M windows."""
from collections import Counter
from datetime import datetime, timezone
import json
import queue
import threading
import time
import uuid

from ..client import Client, canonical, resource
from ..tls import client_context
from .model import Window, validate_batch
from .resources import ORIGIN


class Counters:
    def __init__(self):
        self.values, self.lock = Counter(), threading.Lock()

    def add(self, key, n=1):
        with self.lock:
            self.values[key] += n

    def snapshot(self):
        with self.lock:
            return dict(self.values)


def offer_latest(target, item, counts, dropped):
    try:
        target.put_nowait(item)
    except queue.Full:
        try:
            target.get_nowait()
            counts.add(dropped)
        except queue.Empty:
            pass
        target.put_nowait(item)


class CSEWorker:
    """HTTP never runs in the DDS receive loop. Live windows are not a durable journal."""
    def __init__(self, cfg, settings):
        self.cfg = cfg
        self.client = Client(settings.nodes['edge'], ORIGIN)
        self.pending = queue.Queue(maxsize=8)
        self.counts = Counters()
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.work, name='dds-cse', daemon=True)

    def start(self):
        for name in ('vibration', 'status'):
            if resource(self.client.get(self.client.root + '/ddsIpe/' + name))['ty'] != 3:
                raise ValueError('Run edge bootstrap to configure ddsIpe first')
        self.thread.start()

    def put(self, name, body):
        # Assign identity and expiry before queuing; queue delay cannot extend retention.
        now = time.time()
        offer_latest(self.pending, (name, body, now, uuid.uuid4().hex), self.counts, 'cseQueueDrops')

    def work(self):
        while not self.done.is_set():
            try:
                name, body, at, identity = self.pending.get(timeout=0.2)
            except queue.Empty:
                continue
            expires = at + self.cfg.retention_seconds
            if time.time() >= expires:
                self.counts.add('cseExpiredBeforeWrite')
                continue
            et = datetime.fromtimestamp(expires, timezone.utc).strftime('%Y%m%dT%H%M%S')
            try:
                self.client.create(self.client.root + '/ddsIpe/' + name, 4, {'m2m:cin': {
                    'rn': 'dds-' + identity, 'cnf': 'application/json:0',
                    'con': canonical(body), 'et': et}})
                self.counts.add('cseWritten')
            except Exception as exc:
                self.counts.add('cseWriteFailures')
                # Avoid logging sensor data or untrusted response bodies.
                print(json.dumps({'component': 'dds-ipe', 'writeError': type(exc).__name__}), flush=True)

    def close(self):
        self.done.set()
        self.thread.join(timeout=5)


def endpoint_status(endpoint, writer):
    pairs = [('deadline', 'get_offered_deadline_missed_status' if writer else 'get_requested_deadline_missed_status'),
             ('incompatibleQos', 'get_offered_incompatible_qos_status' if writer else 'get_requested_incompatible_qos_status'),
             ('liveliness', 'get_liveliness_lost_status' if writer else 'get_liveliness_changed_status'),
             ('matched', 'get_publication_matched_status' if writer else 'get_subscription_matched_status')]
    result = {}
    for key, method in pairs:
        status = getattr(endpoint, method)()
        result[key] = {name: int(getattr(status, name)) for name in (
            'total_count', 'total_count_change', 'current_count', 'alive_count',
            'not_alive_count', 'alive_count_change', 'not_alive_count_change', 'last_policy_id')
            if hasattr(status, name)}
    return result


def publisher(cfg, settings, endpoint, stop):
    import paho.mqtt.client as mqtt
    from .wire import VibrationBatch
    incoming, counts = queue.Queue(maxsize=20), Counters()
    source = 'anchor/raw/' + settings.vessel_id + '/engine/vibration'
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id='anchor-dds-' + settings.vessel_id)
    client.tls_set_context(client_context(settings.tls_ca, settings.tls_cert, settings.tls_key))

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            counts.add('mqttConnectFailures')
        else:
            client.subscribe(source, qos=0)

    def on_message(client, userdata, message):
        if message.topic != source or message.retain or len(message.payload) > 8192:
            counts.add('mqttRejected')
            return
        offer_latest(incoming, bytes(message.payload), counts, 'mqttQueueDrops')

    client.on_connect, client.on_message = on_connect, on_message
    client.connect_async(cfg.mqtt_host, cfg.mqtt_port, keepalive=15)
    client.loop_start()
    report_at = 0
    try:
        while not stop.is_set():
            try:
                payload = incoming.get(timeout=0.1)
            except queue.Empty:
                payload = None
            if payload is not None:
                try:
                    value = validate_batch(json.loads(payload), settings.vessel_id,
                                           time.time_ns() // 1000000, cfg.qos.lifespan_ms)
                    endpoint.write(VibrationBatch.from_json(value), timestamp=value['startedAtMs'] * 1000000)
                    counts.add('ddsBatchesWritten')
                    counts.add('ddsSamplesWritten', len(value['xyzG']))
                except (ValueError, TypeError, UnicodeError):
                    counts.add('invalidOrStaleBatches')
                except Exception as exc:
                    counts.add('ddsWriteFailures')
                    print(json.dumps({'component': 'dds-publisher', 'error': type(exc).__name__}), flush=True)
            if time.monotonic() >= report_at:
                print(json.dumps({'component': 'dds-publisher', 'counters': counts.snapshot(),
                                  'dds': endpoint_status(endpoint, True)}), flush=True)
                report_at = time.monotonic() + 5
    finally:
        client.disconnect()
        client.loop_stop()


def ipe(cfg, settings, endpoint, stop):
    worker, window, counts = CSEWorker(cfg, settings), Window(settings.vessel_id, cfg.window_ms), Counters()
    worker.start()
    next_window, next_status = time.monotonic() + cfg.window_ms / 1000, 0
    try:
        while not stop.is_set():
            for sample in endpoint.take(32):
                if not getattr(getattr(sample, 'sample_info', None), 'valid_data', True):
                    continue
                try:
                    value = validate_batch(sample.to_json(), settings.vessel_id,
                                           time.time_ns() // 1000000, cfg.qos.lifespan_ms)
                    window.add(value)
                except (ValueError, TypeError, AttributeError):
                    counts.add('invalidOrStaleBatches')
            now = time.monotonic()
            if now >= next_window:
                body = window.flush(time.time_ns() // 1000000, cfg.qos.describe())
                if body:
                    worker.put('vibration', body)
                next_window = now + cfg.window_ms / 1000
            if now >= next_status:
                body = {'schemaVersion': 1, 'kind': 'ddsStatus', 'observedAt': time.time(),
                        'vesselId': settings.vessel_id, 'qos': cfg.qos.describe(),
                        'dds': endpoint_status(endpoint, False),
                        'counters': {**window.counts, **counts.snapshot(), **worker.counts.snapshot()}}
                worker.put('status', body)
                print(json.dumps(body), flush=True)
                next_status = now + 5
            stop.wait(0.01)
    finally:
        worker.close()


def run(cfg, settings, stop):
    from cyclonedds.core import DDSStatus
    from cyclonedds.domain import Domain, DomainParticipant
    from cyclonedds.topic import Topic
    from cyclonedds.pub import DataWriter
    from cyclonedds.sub import DataReader
    from .wire import VibrationBatch
    domain = Domain(cfg.domain_id, cfg.xml())
    participant = DomainParticipant(cfg.domain_id)
    topic = Topic(participant, cfg.topic, VibrationBatch)
    writer = cfg.mode == 'publisher'
    endpoint = (DataWriter if writer else DataReader)(participant, topic, qos=cfg.qos.policies(writer))
    endpoint.set_status_mask((DDSStatus.OfferedDeadlineMissed | DDSStatus.OfferedIncompatibleQos |
                              DDSStatus.LivelinessLost | DDSStatus.PublicationMatched) if writer else (
                              DDSStatus.RequestedDeadlineMissed | DDSStatus.RequestedIncompatibleQos |
                              DDSStatus.LivelinessChanged | DDSStatus.SubscriptionMatched))
    try:
        (publisher if writer else ipe)(cfg, settings, endpoint, stop)
    finally:
        del endpoint, topic, participant, domain
