"""Select the ship's next hop from MEC013 observations, independently of delivery."""
import time

import requests

from .client import OneM2MError


class EventTarget:
    """One immutable CSE/origin/container/operation/type combination."""

    def __init__(self, client):
        self.client = client
        self.parent = client.root + '/anchor/events'
        self.confirmed = False

    def send(self, rn, body):
        try:
            self.client.create(self.parent, 4, {'m2m:cin': {
                'rn': rn, 'cnf': 'application/json:0', 'con': body}})
        except OneM2MError as exc:
            if exc.http_status in (401, 403) or exc.rsc in (4103, 4107, 5105):
                self.confirmed = False
            elif exc.rsc == 4105:
                path = f'{self.parent}/{rn}'
                if self.client.get(path).get('m2m:cin', {}).get('con') != body:
                    raise ValueError(f'Stored CIN content differs at {path}') from exc
                return 'duplicates'
            raise
        self.confirmed = True
        return 'forwarded'


def edge_for_user(document, address):
    """A missing user, unknown zone or satellite attachment keeps the last route."""
    matches = [u for u in document['userList']['user']
               if address in (u.get('address'), u.get('userId'))]
    if len(matches) != 1:
        return None
    user = matches[0]
    if 'vsat' in str(user.get('accessPointId', '')).lower():
        return None
    return {'kr-busan': 'mep1', 'jp-kyushu': 'mep2'}.get(user.get('zoneId'))


def watch_location(relay):
    cfg = relay.config.handover
    with requests.Session() as session:
        session.trust_env = False
        while not relay.stop.is_set():
            try:
                response = session.get(cfg['location_url'], timeout=2, verify=cfg.get('ca', True),
                                       allow_redirects=False)
                response.raise_for_status()
                edge = edge_for_user(response.json(), cfg.get('address', relay.config.vessel_id))
                if edge:
                    relay.select_edge(edge, time.time())
                with relay.lock:
                    relay.location_error = None
            except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
                with relay.lock:
                    relay.location_error = str(exc)[:240]
            relay.stop.wait(2)
