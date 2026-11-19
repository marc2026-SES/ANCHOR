"""Read-only link: copy sensor CINs from the real ship ACME (Pi) into the sandbox ship CSE.

The real ship CSE is only read (GET as SOURCE_ORIGIN). Nothing is created, updated or deleted there.
Each copied CIN keeps the source resource name, so re-copying is idempotent (4105 = already copied).
Env: SOURCE_SHIP_URL, SOURCE_SHIP_ROOT (cse-ship), SOURCE_CA (PEM for an https source), SHIP_CSE_URL (sandbox mirror), SINCE_MINUTES, LINK_INTERVAL.
"""
import datetime as dt
import os
import time

from anchor.client import Client, OneM2MError, resource
from anchor.config import DEVICES, SENSORS, Endpoint

SRC = Client(Endpoint(os.environ['SOURCE_SHIP_URL'], os.environ.get('SOURCE_SHIP_ROOT', 'cse-ship'), '/id-ship',
                      ca=os.environ.get('SOURCE_CA') or True, timeout=8),
             os.environ.get('SOURCE_ORIGIN', 'CAdmin'))
DST = Client(Endpoint(os.environ['SHIP_CSE_URL'], 'cse-ship', '/id-ship', ca=os.environ.get('SHIP_CA') or True, timeout=8), 'CAdmin')
INTERVAL = float(os.environ.get('LINK_INTERVAL', '3'))
BATCH = int(os.environ.get('LINK_BATCH', '300'))
FMT = '%Y%m%dT%H%M%S'


def log(*a):
    print('[ship-link]', dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).strftime('%H:%M:%S'), *a, flush=True)


since = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(minutes=float(os.environ.get('SINCE_MINUTES', '120')))
cursor = {(d, s): since.strftime(FMT) for d in DEVICES for s in SENSORS[d]}
copied = {k: 0 for k in cursor}
online = None
while True:
    try:
        moved = 0
        for (device, sensor), cra in cursor.items():
            src = f'{SRC.root}/{device}/{sensor}'
            try:
                # One request returns the new CINs with content (rcn=4, fu=2), oldest first.
                # No 'lim': ACME applies lim before the cra filter, so the newest CINs would be skipped.
                cins = SRC.get(src, params={'rcn': 4, 'fu': 2, 'ty': 4, 'cra': cra}).get('m2m:cnt', {}).get('m2m:cin', [])[:BATCH]
            except OneM2MError as exc:
                if exc.rsc == 4004:
                    continue
                raise
            newest = cra
            for cin in cins:
                try:
                    DST.as_origin(DEVICES[device]).create(f'{DST.root}/{device}/{sensor}', 4, {'m2m:cin': {
                        'rn': cin['rn'], 'cnf': cin.get('cnf', 'application/json:0'), 'con': cin['con'],
                        'lbl': ['mirror:source-ct:' + cin['ct']]}})
                    copied[(device, sensor)] += 1
                    moved += 1
                except OneM2MError as exc:
                    if exc.rsc != 4105:
                        raise
                # cra is "created after"; step back to the second of the newest copied CIN (rn dedupes repeats)
                newest = max(newest, (dt.datetime.strptime(cin['ct'][:15], FMT) - dt.timedelta(seconds=1)).strftime(FMT))
            cursor[(device, sensor)] = newest
        if online is not True:
            log('source ship ACME online')
        online = True
        if moved:
            log(f'copied {moved} new CINs; totals', {f'{d}/{s}': n for (d, s), n in copied.items()})
    except Exception as exc:  # source offline/unreachable: keep cursors, retry (store-and-forward)
        if online is not False:
            log('source ship ACME unreachable:', str(exc)[:200])
        online = False
    time.sleep(INTERVAL)
