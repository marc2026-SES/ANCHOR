from __future__ import annotations

import json
import threading
import time
from collections import deque

from . import commands
from .client import Client, OneM2MError, canonical, resource
from .config import ORIGINS, SENSORS
from .events import adapt, digest, fair_batch, number, validate_event
from .incidents import tilt_transitions
from .resources import POLLERS, bootstrap
from .store import Journal


class Runtime:
    def __init__(self, settings, clients=None):
        self.settings = settings
        self.role = settings.role
        self.clients = clients or {k: Client(v, ORIGINS[self.role]) for k, v in settings.nodes.items()}
        self.local = self.clients[self.role]
        self.journal = Journal(self.local, settings)
        self.done = threading.Event()
        self.wake = threading.Event()
        self.ready = False
        self.errors = deque(maxlen=40)
        self.links = {}
        self.lock = threading.RLock()
        self.command_lock = threading.RLock()
        self.last_scan = None
        self.pch_received = 0
        self.notifications = 0
        self.dispatch_status = {}
        self._events = []
        self._receipts = []
        self._commands = []
        self._source_seen = {}  # optimization only, all authority remains in the CSE
        self._last_nudge = {}
        self._sources = (0.0, [])
        self._subscribed = {}
        self._temperature_event = None
        self.threads = []

    @property
    def peers(self):
        if self.role == 'ship':
            return [p for p in ('edge', 'shore') if p in self.clients and (p == 'edge' or self.settings.direct_shore)]
        return ['shore'] if self.role == 'edge' and 'shore' in self.clients else []

    def provision(self):
        result = bootstrap(self.settings, self.local)
        self.ready = True
        self.wake.set()
        return result

    def error(self, scope, exc):
        with self.lock:
            self.errors.append({'at': time.time(), 'scope': scope, 'error': str(exc)[:400]})

    def quarantine(self, identity, reason):
        value = {'identity': identity, 'reason': str(reason)[:300]}
        self.journal.put('quarantine', digest(value), value)

    def values(self, journal, name, validator=None):
        result = []
        for cin in journal.rows(name):
            try:
                value = json.loads(cin['con']) if isinstance(cin['con'], str) else cin['con']
                if not isinstance(value, dict):
                    raise ValueError('Record must be an object')
                if validator:
                    validator(value)
                result.append(value)
            except (ValueError, KeyError, TypeError) as exc:
                self.quarantine(cin.get('ri', 'unknown'), exc)
        return result

    SOURCE_REFRESH_SECONDS = 30

    def sources(self):
        """Labelled hardware containers. The AE/CNT tree is re-discovered every
        SOURCE_REFRESH_SECONDS, so new firmware capabilities still appear without a restart."""
        at, found = self._sources
        if not found or time.monotonic() - at >= self.SOURCE_REFRESH_SECONDS:
            found = list(self.discover_sources())
            self._sources = (time.monotonic(), found)
        return found

    def discover_sources(self):
        for ae_ref in self.local.children(self.local.root):
            ae = resource(self.local.get(ae_ref))
            if ae.get('ty') != 2 or 'anchor:hardware' not in ae.get('lbl', []):
                continue
            device = ae['rn']
            if device not in SENSORS:
                continue
            for ref in self.local.children(ae_ref):
                cnt = resource(self.local.get(ref))
                if cnt.get('ty') == 3 and 'anchor:sensor' in cnt.get('lbl', []):
                    yield ref, device, cnt['rn']

    def ingest(self):
        archived = {e['eventId']: e for e in self.values(self.journal, 'events', validate_event)}
        for path, device, sensor in self.sources():
            try:
                rows = self.local.cins(path)
            except Exception as exc:
                self.error(path, exc)
                continue
            for cin in rows:
                source_key = (cin['ri'], cin['ct'])
                try:
                    payload = json.loads(cin['con']) if isinstance(cin['con'], str) else cin['con']
                    fingerprint = digest(payload)
                    event = adapt(cin, payload, device, sensor, self.settings.vessel_id, self.local.endpoint.csi)
                    validate_event(event)
                    # The journal was read from the CSE in this scan; an identical archived
                    # event needs no further request. A different body still goes through
                    # put() and is rejected there as a conflicting immutable record.
                    if archived.get(event['eventId']) == event:
                        self._source_seen[source_key] = fingerprint
                        continue
                    self.journal.put('events', event['eventId'], event)
                    self._source_seen[source_key] = fingerprint
                except (ValueError, KeyError, TypeError) as exc:
                    self.quarantine('/'.join(source_key), exc)
                # Capacity and CSE failures propagate. Never mark an unarchived source as seen.
            if time.monotonic() - self._subscribed.get(path, -1e9) < self.SOURCE_REFRESH_SECONDS:
                continue
            try:
                subscription = self.local.ensure(path, 23, {'m2m:sub': {
                    'rn': 'anchor-core', 'nu': [self.settings.notify_url], 'enc': {'net': [3]}, 'nct': 1}})
                if resource(subscription).get('nu') != [self.settings.notify_url]:
                    self.local.update(path + '/anchor-core', {'m2m:sub': {'nu': [self.settings.notify_url]}})
                self._subscribed[path] = time.monotonic()
            except Exception as exc:
                self.error('subscription:' + path, exc)  # historical scan still works

    def local_tick(self):
        with self.journal.batch():
            self._local_tick()

    def _local_tick(self):
        if self.role == 'ship':
            self.ingest()
        events = self.values(self.journal, 'events', validate_event)
        if self.role == 'ship':
            for event in tilt_transitions(events, self.settings):
                if not any(e['eventId'] == event['eventId'] for e in events):
                    self.journal.put('events', event['eventId'], event)
            events = self.values(self.journal, 'events', validate_event)
            climate = [e for e in events if e['kind'] == 'measurement' and e['sensor'] == 'climate'
                       and e['payload'].get('ok') is True and number(e['payload'].get('temp_c'))]
            if climate:
                newest = max(climate, key=lambda e: e['generatedAt'])
            if climate and newest['eventId'] != self._temperature_event:
                self.local.update(self.local.root + '/cargo/temperature', {'cod:tempe': {
                    'curT0': newest['payload']['temp_c'], 'lbl': ['anchor:sdt', 'quality:observed',
                                                                               'event:' + newest['eventId']]}})
                self._temperature_event = newest['eventId']
        if self.role in ('edge', 'shore'):
            existing = {(r.get('holder'), r.get('eventId')): r for r in self.values(self.journal, 'receipts')}
            for event in events:
                # A receipt is issued only by the receiving runtime, after reading
                # its own persisted event and validating its immutable envelope.
                receipt = {'eventId': event['eventId'], 'hash': digest(event), 'holder': self.role,
                           'stage': 'final' if self.role == 'shore' else 'custody'}
                if existing.get((self.role, event['eventId'])) != receipt:
                    self.journal.put('receipts', self.role + ':' + event['eventId'], receipt)
        queued = self.values(self.journal, 'commands', commands.validate)
        receipts = self.values(self.journal, 'receipts')
        with self.lock:
            self._events, self._receipts, self._commands = events, receipts, queued
            self.last_scan = time.time()

    def transfer_tick(self, peer):
        remote = Journal(self.clients[peer], self.settings)
        with remote.batch(), self.journal.batch():
            self._transfer_tick(peer, remote)

    def _transfer_tick(self, peer, remote):
        with self.lock:
            events = list(self._events)
        lookup = {event['eventId']: event for event in events}
        remote_events = {e['eventId']: e for e in self.values(remote, 'events', validate_event)}
        # Forward first. An event the peer does not hold yet must not wait behind receipt
        # bookkeeping for older events; an identical event already stored there only waits
        # for its receipt and is not sent again.
        unsent = [e for e in events if remote_events.get(e['eventId']) != e]
        for event in fair_batch(unsent, self.settings.batch_size, self.settings.critical_burst):
            try:
                remote.put('events', event['eventId'], event)
            except ValueError as exc:
                self.quarantine('forward:' + event['eventId'], exc)
        # Then reconcile receipts. Every receipt is still verified below; only the local copy
        # is written a bounded number per tick, so a receipt backlog cannot stall forwarding.
        remote_receipts = self.values(remote, 'receipts')
        local_receipts = {(r.get('holder'), r.get('eventId')): r for r in self.values(self.journal, 'receipts')}
        accepted = set()
        copied = 0
        for receipt in remote_receipts:
            event = lookup.get(receipt.get('eventId'))
            if event is None:
                continue
            holder = receipt.get('holder')
            if holder not in ('edge', 'shore') or receipt.get('stage') != ('final' if holder == 'shore' else 'custody'):
                self.quarantine('receipt:' + event['eventId'], 'Invalid receipt holder/stage')
                continue
            if digest(event) != receipt.get('hash'):
                self.quarantine('receipt:' + event['eventId'], 'Receipt hash mismatch')
                continue
            # Verify peer custody against the peer's current event. A deleted CSE
            # cannot keep suppressing replay using a stale receipt record alone.
            if holder == peer:
                stored = remote_events.get(event['eventId'])
                if stored != event:
                    continue
                accepted.add(event['eventId'])
            if local_receipts.get((holder, event['eventId'])) != receipt and copied < self.settings.batch_size:
                self.journal.put('receipts', holder + ':' + event['eventId'], receipt)
                copied += 1
        pending = [e for e in events if e['eventId'] not in accepted]
        with self.lock:
            self.links[peer] = {'reachable': True, 'observedAt': time.time(), 'pendingAtPeer': len(pending)}

    def cache_commands_tick(self, peer):
        remote = Journal(self.clients[peer], self.settings)
        existing = {c['commandId']: c for c in self.values(self.journal, 'commands', commands.validate)}
        for command in self.values(remote, 'commands', commands.validate):
            try:
                if existing.get(command['commandId']) != command:
                    self.journal.put('commands', command['commandId'], command)
            except ValueError as exc:
                self.quarantine('command:' + command['commandId'], exc)
        # Expired commands are retained for an honest audit, never silently executed.

    def command_tick(self):
        if self.role != 'ship':
            return
        with self.lock:
            queued, events = list(self._commands), list(self._events)
        results = commands.result_index(events, queued)
        for command in sorted(queued, key=lambda c: (c['issuedAtMs'], c['commandId'])):
            try:
                status = commands.dispatch(self.local, self.journal, command, int(time.time() * 1000), results)
                with self.lock:
                    self.dispatch_status[command['commandId']] = status
                # Re-notify incomplete devices even when their SUB notification was
                # lost. Firmware deduplication preserves the original lease deadline.
                if status == 'dispatched':
                    for device in commands.targets(command):
                        if (command['commandId'], device) in results:
                            continue
                        key = (command['commandId'], device)
                        if time.monotonic() - self._last_nudge.get(key, 0) < 5:
                            continue
                        self._last_nudge[key] = time.monotonic()
                        self.local.notify(self.local.root + '/' + device, {'m2m:sgn': {
                            'sur': self.local.root + '/' + device + '/command',
                            'nev': {'net': 3, 'rep': {'m2m:cin': {'con': canonical(command)}}}}})
            except (ValueError, KeyError) as exc:
                self.quarantine('dispatch:' + command.get('commandId', 'invalid'), exc)

    def submit_command(self, body):
        if self.role != 'shore':
            raise ValueError('Submit commands at shore')
        with self.command_lock:
            command = commands.make(body)
            existing = self.journal.get('commands', command['commandId'])
            if existing:
                for key in ('target', 'action', 'durationMs', 'intervalMs'):
                    if existing[key] != command[key]:
                        raise ValueError('commandId already has a different payload')
                if existing['expiresAtMs'] - existing['issuedAtMs'] != command['expiresAtMs'] - command['issuedAtMs']:
                    raise ValueError('commandId already has a different TTL')
                return existing
            self.journal.put('commands', command['commandId'], command)
            self.wake.set()
        return command

    def hint_tick(self):
        if self.role not in POLLERS:
            return
        name, _ = POLLERS[self.role]
        # A single bounded blocking PCH notification per worker, not a thread per command.
        with self.lock:
            queued = list(self._commands)
        if not any(int(time.time() * 1000) < c['expiresAtMs'] for c in queued):
            return
        self.local.notify(self.local.root + '/' + name, {'m2m:sgn': {
            'sur': self.local.root + '/anchor/commands', 'nev': {'net': 3, 'rep': {'m2m:cin': {'con': '{}'}}}}})

    def poll_tick(self, peer):
        if peer not in POLLERS:
            return
        name, origin = POLLERS[peer]
        # Ship polls its reachable coastal CSE, not a shore endpoint behind that CSE.
        if (self.role, peer) not in (('ship', 'edge'), ('edge', 'shore')):
            return
        client = self.clients[peer].as_origin(origin)
        path = client.root + '/' + name + '/pch/pcu'
        try:
            content = client.get(path, headers={'X-M2M-RET': '1000'})
        except OneM2MError as exc:
            if exc.rsc in (4008, 5103):
                return
            raise
        for request in content.get('m2m:agrp', [content]):
            request = request.get('m2m:rqp', request)
            client.notify(path, {'m2m:rsp': {'rqi': request['rqi'], 'rsc': 2000, 'fr': origin,
                                           'to': request['fr'], 'rvi': client.endpoint.rvi}})
            with self.lock:
                self.pch_received += 1
            self.wake.set()

    def acknowledge(self, incident_id, operator):
        if self.role not in ('edge', 'shore') or not isinstance(operator, str) or not 1 <= len(operator.strip()) <= 80:
            raise ValueError('An edge/shore operator name is required')
        with self.lock:
            incident = next((e for e in self._events if e['kind'] == 'incident' and e['payload']['incidentId'] == incident_id), None)
        if incident is None:
            raise ValueError('Incident has not arrived at this site')
        identity = incident_id + ':ack:' + self.role
        existing = self.journal.get('events', identity)
        if existing:
            return existing
        event = {'schemaVersion': 2, 'eventId': identity, 'vesselId': incident['vesselId'],
                 'deviceId': self.role + '-operator', 'bootId': 'operator', 'generatedAt': time.time(),
                 'kind': 'incidentAck', 'priority': 'critical',
                 'payload': {'incidentId': incident_id, 'operator': operator.strip(), 'site': self.role}}
        self.journal.put('events', identity, event)
        self.wake.set()
        return event

    def capture_batch(self, name):
        with self.lock:
            expected = {e['eventId']: digest(e) for e in self._events}
            scanned = self.last_scan
        if not expected or not scanned:
            raise ValueError('No scanned events to capture')
        value = {'batchId': name, 'expected': expected, 'capturedAt': time.time(), 'sourceScanAt': scanned}
        self.journal.put('audit', 'batch:' + name, value)
        return value

    def status(self):
        with self.lock:
            events, receipts, queued = list(self._events), list(self._receipts), list(self._commands)
            state = {'role': self.role, 'ready': self.ready, 'lastScanAt': self.last_scan,
                     'links': dict(self.links), 'errors': list(self.errors)[-8:],
                     'pollingChannelReceived': self.pch_received, 'notificationRequests': self.notifications}
        final = {r['eventId']: r['hash'] for r in receipts if r.get('holder') == 'shore' and r.get('stage') == 'final'}
        custody = {r['eventId']: r['hash'] for r in receipts if r.get('holder') == 'edge'}
        state['delivery'] = {'accepted': len(events),
                             'coastCustody': sum(custody.get(e['eventId']) == digest(e) for e in events),
                             'shoreConfirmed': sum(final.get(e['eventId']) == digest(e) for e in events)}
        state['delivery']['pendingFinal'] = len(events) - state['delivery']['shoreConfirmed']
        state['incidents'] = [e for e in events if e['kind'] in ('incident', 'incidentAck')][-30:]
        latest = {}
        streams = {}
        for e in events:
            if e['kind'] == 'measurement':
                key = e['deviceId'] + '/' + e['sensor']
                if key not in latest or latest[key]['generatedAt'] < e['generatedAt']:
                    latest[key] = e
            if e['kind'] in ('measurement', 'commandResult') and e.get('quality', {}).get('identity') == 'device-boot-sequence':
                streams.setdefault((e['deviceId'], e['bootId']), set()).add(e['seq'])
        state['latest'] = latest
        state['sequenceGaps'] = {':'.join(k): max(v) + 1 - len(v) for k, v in streams.items()}
        results = commands.result_index(events, queued)
        command_status = []
        for command in queued[-30:]:
            device_results = {d: results[(command['commandId'], d)]['payload'] for d in commands.targets(command)
                              if (command['commandId'], d) in results}
            state_name = self.dispatch_status.get(command['commandId'], 'cached')
            if len(device_results) == len(commands.targets(command)):
                state_name = 'applied' if all(r.get('status') == 'applied' for r in device_results.values()) else 'device-reported'
            elif int(time.time() * 1000) >= command['expiresAtMs']:
                state_name = 'expired-unconfirmed'
            command_status.append({**command, 'status': state_name, 'devices': device_results})
        state['commands'] = command_status
        return state

    def batch_status(self, name):
        batch = self.journal.get('audit', 'batch:' + name)
        if batch is None:
            raise ValueError('Unknown batch')
        receipts = self.values(self.journal, 'receipts')
        final = {r['eventId']: r['hash'] for r in receipts if r.get('holder') == 'shore' and r.get('stage') == 'final'}
        pending = [eid for eid, h in batch['expected'].items() if final.get(eid) != h]
        return {'batchId': name, 'expected': len(batch['expected']), 'pending': pending, 'complete': not pending}

    def _worker(self, scope, fn, interval=None, peer=None):
        while not self.done.is_set():
            if self.ready:
                try:
                    fn()
                except Exception as exc:
                    self.error(scope, exc)
                    if peer and scope.startswith('transfer'):
                        with self.lock:
                            self.links[peer] = {'reachable': False, 'observedAt': time.time(), 'error': str(exc)[:200]}
            delay = self.settings.scan_seconds if interval is None else interval
            if scope == 'local':
                self.wake.wait(delay)
                self.wake.clear()
            else:
                self.done.wait(delay)

    def start(self):
        jobs = [('local', self.local_tick, self.settings.scan_seconds, None)]
        if self.role == 'ship':
            jobs.append(('dispatch', self.command_tick, 2, None))
        if self.role in POLLERS:
            jobs.append(('hint', self.hint_tick, 3, None))
        for peer in self.peers:
            jobs.extend([(f'transfer:{peer}', lambda p=peer: self.transfer_tick(p), 1, peer),
                         (f'commands:{peer}', lambda p=peer: self.cache_commands_tick(p), 2, peer),
                         (f'pch:{peer}', lambda p=peer: self.poll_tick(p), 0.2, peer)])
        for scope, fn, interval, peer in jobs:
            thread = threading.Thread(target=self._worker, args=(scope, fn, interval, peer), daemon=True, name=scope)
            thread.start()
            self.threads.append(thread)

    def stop(self):
        self.done.set()
        self.wake.set()
        for thread in self.threads:
            thread.join(timeout=5)
