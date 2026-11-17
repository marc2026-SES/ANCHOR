"""Separate operator credentials from the monitor's status-only credential."""
import copy
import hmac
from pathlib import Path


def read_token(filename):
    token = Path(filename).read_text().strip()
    if len(token) < 24:
        raise ValueError('API tokens must contain at least 24 characters')
    return token


class ApiTokens:
    def __init__(self, operator, status=''):
        if len(operator) < 24 or (status and len(status) < 24):
            raise ValueError('API tokens must contain at least 24 characters')
        if status and hmac.compare_digest(operator.encode(), status.encode()):
            raise ValueError('Operator and status tokens must be different')
        self.operator, self.status = operator, status

    def identify(self, authorization):
        supplied = authorization.encode('utf-8')
        if hmac.compare_digest(supplied, ('Bearer ' + self.operator).encode()):
            return 'operator'
        if self.status and hmac.compare_digest(supplied, ('Bearer ' + self.status).encode()):
            return 'status'
        return None


def monitor_status(state):
    """Only continuity counters, not location, raw events or diagnostic bodies."""
    view = {key: copy.deepcopy(state[key]) for key in (
        'role', 'ready', 'lastScanAt', 'delivery', 'sequenceGaps',
        'pollingChannelReceived', 'notificationRequests') if key in state}
    view['links'] = {peer: {key: values[key] for key in ('reachable', 'observedAt', 'pendingAtPeer') if key in values}
                     for peer, values in state.get('links', {}).items()}
    return view
