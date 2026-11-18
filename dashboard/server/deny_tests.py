#!/usr/bin/env python3
"""Denied-request tests T1–T10 from the ACP doc, against the real CSEs.

    python3 server/deny_tests.py --config server/shore-monitor.json          # all tests
    python3 server/deny_tests.py --config server/shore-monitor.json --only T2,T8

Uses the Shore Monitor config (shore, edge, ship CSEs, cores.ship.url for T7) and writes
src/security/deny-results.json, which the dashboard Security tab shows. That file holds test ids,
result codes and CSE names only (no addresses), so it can be committed as evidence.

Each test sends one request under the originator it is about (ACP checks only the X-M2M-Origin
header; see the ACP doc, principle 6). Requests that would change data are made harmless:
  - writes carry a body the core quarantines and the ESP32 ignores (no commandId), and are deleted
    again by the owning core identity if a broken ACP lets them through;
  - T5 (DELETE a shore record) backs the record up to server/data/ first and re-creates it if the
    delete goes through;
  - T6 updates the receipts container with the labels it already has, so nothing changes;
  - T10's subscription points at a closed port, so it can never receive anything.
"""
import argparse
import json
import secrets
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from shore_monitor import CSE, load_config, make_opener, resource  # noqa: E402

HERE = Path(__file__).parent
DEFAULT_OUT = HERE.parent / 'src' / 'security' / 'deny-results.json'
MARK = {'denyTest': True, 'note': 'ACP deny test, must be refused'}


class Site:
    """One CSE, with a client per originator."""

    def __init__(self, node):
        self.node, self.root, self.clients = node, node['root'], {}

    def as_(self, origin):
        if origin not in self.clients:
            self.clients[origin] = CSE(self.node, origin)
        return self.clients[origin]


def exists(cse, target):
    """True / False, or None when the answer is neither found nor not-found (no read right, error)."""
    rsc, _ = cse.exchange('GET', target)
    return True if rsc == 2000 else False if rsc == 4004 else None


def latest(cse, container):
    """The newest content instance of a container, or None."""
    rsc, body = cse.exchange('GET', container + '/la')
    return resource(body) if rsc == 2000 else None


def remove(owner, target):
    """Delete something a broken ACP let a test create, then confirm it is gone."""
    owner.exchange('DELETE', target)
    gone = exists(owner, target) is False
    return 'created, then deleted again' if gone else f'created, CLEANUP FAILED: delete {target} by hand'


# ── tests ─────────────────────────────────────────────────────────────────────
# Every check looks at the state afterwards (does the thing exist?), not only at the response code.

def write_test(site, origin, container, owner):
    """CREATE a marked CIN; expect 4103. If it goes through, the owning core deletes it again."""
    def run():
        name = 'denytest-' + secrets.token_hex(6)
        body = {'m2m:cin': {'rn': name, 'cnf': 'application/json:0', 'con': json.dumps(MARK)}}
        rsc, _ = site.as_(origin).exchange('POST', container, body, 4)
        made = exists(site.as_(owner), f'{container}/{name}')
        return rsc, (remove(site.as_(owner), f'{container}/{name}') if made is not False else '')
    return run


def read_test(site, origin, target):
    return lambda: (site.as_(origin).exchange('GET', target)[0], '')


def delete_record_test(site, origin, container, owner, backup_dir):
    """DELETE the newest record. It is backed up first and re-created if it is gone afterwards;
    the backup file stays unless the record is confirmed back in place."""
    def run():
        cin = latest(site.as_(owner), container)
        if cin is None:
            return None, 'no record to aim at (journal empty or not readable)'
        target = f'{container}/{cin["rn"]}'
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f'deny-T5-backup-{int(time.time())}.json'
        backup.write_text(json.dumps({'container': container, 'cin': cin}, ensure_ascii=False, indent=1))
        rsc, _ = site.as_(origin).exchange('DELETE', target)
        if exists(site.as_(owner), target):
            backup.unlink()
            return rsc, ''
        keep = {k: cin[k] for k in ('cnf', 'con', 'lbl') if k in cin}
        site.as_(origin).exchange('POST', container, {'m2m:cin': {'rn': cin['rn'], **keep}}, 4)
        if exists(site.as_(owner), target):
            backup.unlink()
            return rsc, f'deleted, then re-created {cin["rn"]}'
        return rsc, f'deleted, RE-CREATE FAILED; backup in {backup}'
    return run


def update_journal_test(site, origin, container):
    """UPDATE the journal container with its own current labels, so even a broken ACP changes nothing.
    Not a record: ACME answers 4005 to any record UPDATE before it looks at the ACP (records never change),
    so a record UPDATE says nothing about permissions. The container is what a site could really tamper
    with (mni, labels, acpi)."""
    def run():
        rsc, body = site.as_(origin).exchange('GET', container)  # R is granted to the coast core
        if rsc != 2000:
            return None, f'cannot read {container} first ({rsc})'
        labels = resource(body).get('lbl') or []
        rsc, _ = site.as_(origin).exchange('PUT', container, {'m2m:cnt': {'lbl': labels}})
        return rsc, ''
    return run


def subscribe_test(site, origin, target, owner):
    def run():
        name = 'denytest-' + secrets.token_hex(6)
        body = {'m2m:sub': {'rn': name, 'nu': ['https://127.0.0.1:9/denytest'], 'enc': {'net': [3]}, 'nct': 1}}
        rsc, _ = site.as_(origin).exchange('POST', target, body, 23)
        made = exists(site.as_(owner), f'{target}/{name}')
        if made is not False:
            return rsc, remove(site.as_(owner), f'{target}/{name}')
        # ACME checks the ACP before it tries the notification address, so 5xxx here means the ACP let it through
        return rsc, ('ACP let it through; not created only because the test address is closed' if rsc >= 5000 else '')
    return run


def core_without_token(url, ca, timeout):
    def run():
        req = urllib.request.Request(url.rstrip('/') + '/status', method='GET')
        try:
            with make_opener(ca).open(req, timeout=timeout) as response:
                return response.status, ''
        except urllib.error.HTTPError as exc:
            return exc.code, ''
    return run


def build(cfg):
    ship, shore = Site(cfg['ship']), Site(cfg['shore'])
    s, h = ship.root, shore.root
    backup_dir = Path(cfg['db']).parent
    core = cfg['cores']['ship']['url'] or cfg['cores']['edge']['url']
    tests = [
        ('T1', 'Dashboard writes sensor data', 'CAnchorDashboard', f'CREATE cin · {s}/bridge/attitude', 4103, 'ship',
         write_test(ship, 'CAnchorDashboard', f'{s}/bridge/attitude', 'CAnchorShip')),
        ('T2', 'Dashboard reads the position', 'CAnchorDashboard', f'RETRIEVE {s}/bridge/position', 4103, 'ship',
         read_test(ship, 'CAnchorDashboard', f'{s}/bridge/position')),
        ('T3', 'Node writes another node', 'CEngine', f'CREATE cin · {s}/cargo/climate', 4103, 'ship',
         write_test(ship, 'CEngine', f'{s}/cargo/climate', 'CAnchorShip')),
        ('T4', 'Node sends a command', 'CEngine', f'CREATE cin · {s}/bridge/command', 4103, 'ship',
         write_test(ship, 'CEngine', f'{s}/bridge/command', 'CAnchorShip')),
        ('T5', 'Ship deletes a shore record', 'CAnchorShip', f'DELETE · {h}/anchor/events/<newest>', 4103, 'shore',
         delete_record_test(shore, 'CAnchorShip', f'{h}/anchor/events', 'CAnchorShore', backup_dir)),
        ('T6', 'Coast changes the shore receipts journal', 'CAnchorEdge', f'UPDATE · {h}/anchor/receipts', 4103, 'shore',
         update_journal_test(shore, 'CAnchorEdge', f'{h}/anchor/receipts')),
        ('T7', 'Control API without the token', 'any client', 'GET <core>/status', 401, 'core',
         core_without_token(core, cfg['tls']['ca'], 3) if core else None),
        ('T8', 'Control: dashboard reads the shore journal', 'CAnchorDashboard', f'RETRIEVE {h}/anchor/events', 2000, 'shore',
         read_test(shore, 'CAnchorDashboard', f'{h}/anchor/events')),
        ('T9', 'Dashboard writes into the shore journal', 'CAnchorDashboard', f'CREATE cin · {h}/anchor/events', 4103, 'shore',
         write_test(shore, 'CAnchorDashboard', f'{h}/anchor/events', 'CAnchorShore')),
        ('T10', 'Dashboard subscribes outside anchor/events', 'CAnchorDashboard', f'CREATE sub · {h}/anchor/receipts', 4103, 'shore',
         subscribe_test(shore, 'CAnchorDashboard', f'{h}/anchor/receipts', 'CAnchorShore')),
    ]
    urls = {'ship': cfg['ship']['url'], 'shore': cfg['shore']['url'], 'core': core}
    return tests, urls


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--config', required=True)
    ap.add_argument('--only', help='comma-separated ids, e.g. T2,T8')
    ap.add_argument('--out', default=str(DEFAULT_OUT), help='results file (default: src/security/deny-results.json)')
    args = ap.parse_args()

    cfg = load_config(args.config)
    tests, urls = build(cfg)
    only = {t.strip().upper() for t in args.only.split(',')} if args.only else None
    out = Path(args.out)
    previous = {}
    if only and out.exists():  # keep earlier results of the tests not re-run
        previous = {r['id']: r for r in json.loads(out.read_text(encoding='utf-8')).get('results', [])}

    results = []
    print(f'{"":4} {"test":44} {"expect":>6} {"got":>6}  result')
    for tid, title, who, request, expect, where, run in tests:
        if only and tid not in only:
            if tid in previous:
                results.append(previous[tid])
            continue
        row = {'id': tid, 'expected': expect, 'got': None, 'status': 'skipped', 'note': '',
               'at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
        if run is None or not urls.get(where):
            row['note'] = f'{where} address not set in the config'
        else:
            try:
                got, note = run()
                row['got'], row['note'] = got, note
                row['status'] = 'skipped' if got is None else ('pass' if got == expect else 'fail')
            except (urllib.error.URLError, OSError) as exc:
                row['status'], row['note'] = 'error', f'unreachable: {getattr(exc, "reason", exc)}'
        results.append(row)
        print(f'{tid:4} {title[:44]:44} {expect:>6} {str(row["got"] or "-"):>6}  {row["status"]}'
              + (f'  ({row["note"]})' if row['note'] else ''))

    cses = {k: cfg[k]['root'] for k in ('ship', 'shore')}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({'ranAt': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'cses': cses,
                               'results': sorted(results, key=lambda r: int(r['id'][1:]))},
                              ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    failed = [r['id'] for r in results if r['status'] in ('fail', 'error')]
    print(f'\nwrote {out}' + (f'  ·  not as expected: {", ".join(failed)}' if failed else '  ·  all as expected'))
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
