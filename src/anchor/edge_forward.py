"""Forward immutable CINs from an ACME edge journal to shore, preserving rn and con."""
import sqlite3
import threading
import time
from pathlib import Path

from .client import Client, OneM2MError, resource
from .handover import EventTarget

WINDOW = 50


class EdgeForwarder:
    def __init__(self, config, state_path):
        self.config = config
        self.local = Client(config.nodes['edge'], config.origin)
        self.target = EventTarget(Client(config.nodes['shore'], config.origin))
        self.parent = self.local.root + '/anchor/events'
        Path(state_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(state_path, check_same_thread=False, isolation_level=None)
        self.db.execute('CREATE TABLE IF NOT EXISTS cursor (stream TEXT PRIMARY KEY, ri TEXT, st INTEGER)')
        # A change of downstream CSE starts a separate delivery cursor.
        self.stream = '|'.join((self.local.endpoint.csi, self.target.client.endpoint.url,
                                self.target.client.endpoint.csi, self.target.parent))
        row = self.db.execute('SELECT ri, st FROM cursor WHERE stream=?', (self.stream,)).fetchone()
        self.ri, self.st = row or (None, 0)
        self.stop, self.wake = threading.Event(), threading.Event()
        self.lock = threading.Lock()
        self.hints = {}
        self.idle_snapshot = None
        self.sources = {}
        self.last_stored = self.error = None
        self.shore_link = {}

    def discover(self):
        cnt = resource(self.local.get(self.parent))
        sub = resource(self.local.ensure(self.parent, 23, {'m2m:sub': {
            'rn': 'anchor-shore-forward', 'nu': [self.config.notify_url],
            'enc': {'net': [3]}, 'nct': 1}}))
        if sub.get('nu') != [self.config.notify_url]:
            self.local.update(self.parent + '/anchor-shore-forward', {'m2m:sub': {'nu': [self.config.notify_url]}})
        self.sources = {cnt['ri']: self.parent}
        self.wake.set()
        return self.sources

    def accept(self, cin):
        # Notifications can be missed or reordered. Read the authoritative CIN at
        # the edge before sending; the bounded hints only avoid discovery scans.
        st, rn = cin.get('st'), cin.get('rn')
        if cin.get('pi') not in self.sources or type(st) is not int or not isinstance(rn, str) or '/' in rn:
            return False
        with self.lock:
            if len(self.hints) < WINDOW and st > self.st:
                self.hints[st] = rn
        self.wake.set()
        return True

    def checkpoint(self, ri, st):
        self.db.execute('INSERT OR REPLACE INTO cursor VALUES (?, ?, ?)', (self.stream, ri, st))
        with self.lock:
            self.ri, self.st = ri, st
            self.hints = {tag: rn for tag, rn in self.hints.items() if tag > st}

    def send_cin(self, cin):
        if cin['pi'] != self.ri or not isinstance(cin['con'], str):
            raise ValueError('Invalid edge CIN')
        try:
            self.target.send(cin['rn'], cin['con'])
        except Exception:
            self.shore_link = {'reachable': False, 'observedAt': time.time()}
            raise
        self.last_stored = time.time()
        self.shore_link = {'reachable': True, 'observedAt': self.last_stored}

    def advance(self):
        """Drain one range. One worker owns the cursor, and advances only after delivery."""
        with self.lock:
            rn = self.hints.get(self.st + 1)
        if rn:
            try:
                cin = resource(self.local.get(f'{self.parent}/{rn}'))
            except OneM2MError as exc:
                if exc.rsc != 4004:
                    raise
                cin = {}  # stale hint, recover via the local journal

            if cin.get('pi') == self.ri and cin.get('st') == self.st + 1:
                self.send_cin(cin)
                self.checkpoint(self.ri, cin['st'])
                return True
        cnt = resource(self.local.get(self.parent))
        ri, high = cnt['ri'], cnt['st']
        if ri != self.ri:
            with self.lock:
                self.hints.clear()
            self.checkpoint(ri, 0)
        if high < self.st:
            raise ValueError('Edge state tag moved backwards')
        snapshot = (ri, high, cnt['cni'])
        if high == self.st or snapshot == self.idle_snapshot:
            return False
        upper = min(high, self.st + WINDOW)
        # This pinned ACME release compares st > sts and st < stb.
        # Do not use lim/ofst: ACME applies them before filtering.
        params = {'rcn': 4, 'lvl': 1, 'ty': 4, 'stb': upper + 1}
        if self.st:
            params['sts'] = self.st
        window = self.local.get(self.parent, params=params)['m2m:cnt']
        if window['ri'] != ri:
            return True  # Container replaced during discovery. Read its identity again.
        rows = sorted(window.get('m2m:cin', []), key=lambda c: c['st'])
        for cin in rows:
            if not self.st < cin['st'] <= upper:
                raise ValueError('Edge CSE returned a CIN outside the requested window')
            self.send_cin(cin)
        # Some CNT updates increment st without creating a CIN. A successful
        # bounded query covers these gaps too. A failed shore write replays the window.
        # ACME updates the parent's st just before persisting the CIN's st.
        # Never checkpoint a missing tail at the current high-water mark.
        if upper == high and (not rows or rows[-1]['st'] < high):
            if rows:
                self.checkpoint(ri, rows[-1]['st'])
            # An unfinished CREATE changes cni when the CIN is committed. A
            # metadata-only update does not warrant another discovery every poll.
            self.idle_snapshot = snapshot
            return False
        self.checkpoint(ri, upper)
        return True

    def forward_loop(self):
        pause = 0.5
        while not self.stop.is_set():
            self.wake.clear()
            try:
                more = self.advance()
                self.error = None
                pause = 0.5
                if not more:
                    self.wake.wait(2)
            except Exception as exc:
                self.error = str(exc)[:300]
                self.stop.wait(pause)
                pause = min(pause * 2, 5)

    def status(self):
        return {'role': 'edge', 'ready': bool(self.sources), 'cse': self.local.root,
                'forwardedThroughStateTag': self.st, 'lastShoreStoreAt': self.last_stored,
                'links': {'shore': self.shore_link}, 'lastError': self.error}
