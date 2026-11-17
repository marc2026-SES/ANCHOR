// ACPs from the ACP design doc: what `anchor ... bootstrap` (src/anchor/resources.py) provisions,
// renamed acp-<resource path> (one ACP per resource), plus the grants marked ★.
// Every ACP's self-privileges (pvs) go to CAdmin only.
// Ops: C create(1) · R retrieve(2) · U update(4) · D delete(8) · N notify(16) · Di discover(32) · all = 63

export interface Grant { who: string; ops: string }
export interface AcpRow { name: string; on: string; cse: string; grants: Grant[] }

export const ACPS: AcpRow[] = [
  { name: 'acp-ddsIpe', on: 'native DDS IPE AE (edge option)', cse: 'coast',
    grants: [{ who: 'CAnchorDDSIPE · CAnchorEdge · CAnchorDashboard', ops: 'R Di' }] },
  { name: 'acp-ddsIpe-vibration · acp-ddsIpe-status', on: 'raw vibration windows and DDS status; no command or main-journal access', cse: 'coast',
    grants: [{ who: 'CAnchorDDSIPE', ops: 'R Di C(CIN)' }, { who: 'CAnchorEdge · CAnchorDashboard', ops: 'R Di' }] },
  { name: 'acp-anchor · acp-anchor-dispatches · -quarantine · -ruleState · -audit', on: 'anchor (core AE) and internal journals', cse: 'ship · coast · shore',
    grants: [{ who: 'core of that site', ops: 'all' }] },
  { name: 'acp-anchor-events', on: 'anchor/events', cse: 'ship',
    grants: [{ who: 'CAnchorShip', ops: 'all' }] },
  { name: 'acp-anchor-events', on: 'anchor/events', cse: 'coast',
    grants: [{ who: 'CAnchorEdge', ops: 'all' }, { who: 'CAnchorShip', ops: 'C R Di' }, { who: 'CAnchorDashboard (dashboard_edge_read)', ops: 'R Di' }] },
  { name: 'acp-anchor-events', on: 'anchor/events', cse: 'shore',
    grants: [{ who: 'CAnchorShore', ops: 'all' }, { who: 'CAnchorEdge · CAnchorShip', ops: 'C R Di' }, { who: 'CAnchorDashboard', ops: 'R Di C(SUB)' }] },
  { name: 'acp-anchor-receipts · acp-anchor-commands', on: 'anchor/receipts · anchor/commands', cse: 'coast',
    grants: [{ who: 'CAnchorEdge', ops: 'all' }, { who: 'CAnchorShip', ops: 'R Di' }] },
  { name: 'acp-anchor-commands', on: 'anchor/commands (optional monitor read)', cse: 'coast',
    grants: [{ who: 'CAnchorDashboard (dashboard_edge_read)', ops: 'R Di' }] },
  { name: 'acp-anchor-receipts · acp-anchor-commands', on: 'anchor/receipts · anchor/commands', cse: 'shore',
    grants: [{ who: 'CAnchorShore', ops: 'all' }, { who: 'CAnchorEdge · CAnchorShip', ops: 'R Di' }, { who: '★ CAnchorDashboard', ops: 'R Di' }] },
  { name: 'acp-shipCommands · acp-edgeCommands', on: 'poller AE + pollingChannel (coast · shore)', cse: 'coast · shore',
    grants: [{ who: 'core · CAnchorShipPoll / CAnchorEdgePoll', ops: 'all' }] },
  { name: 'acp-<node> · acp-<node>-<sensor> · acp-<node>-commandResults', on: 'bridge · engine · cargo AEs and their sensor containers', cse: 'ship',
    grants: [{ who: 'CAnchorShip', ops: 'all' }, { who: 'CBridge · CEngine · CCargo (own node)', ops: 'C R U' }, { who: 'CAnchorDashboard', ops: 'R' }] },
  { name: 'acp-bridge-position', on: 'bridge/position', cse: 'ship',
    grants: [{ who: 'CAnchorShip', ops: 'all' }, { who: 'CBridge', ops: 'C R U' }] },
  { name: 'acp-bridge-command · acp-engine-command', on: '<node>/command (reportingGroup members)', cse: 'ship',
    grants: [{ who: 'CAnchorShip', ops: 'all' }, { who: 'CBridge · CEngine (own node)', ops: 'R C(SUB)' }] },
  { name: 'acp-anchor-reportingGroup', on: 'anchor/reportingGroup', cse: 'ship',
    grants: [{ who: 'CAnchorShip', ops: 'all' }] },
]

/** Denied-request tests. Results stay "not run" until executed against the real CSEs. */
export const DENY_TESTS: { id: string; test: string; who: string; request: string; expect: string }[] = [
  { id: 'T1', test: 'Dashboard writes sensor data', who: 'CAnchorDashboard', request: 'CREATE cin · ship bridge/attitude', expect: '4103' },
  { id: 'T2', test: 'Dashboard reads the position', who: 'CAnchorDashboard', request: 'RETRIEVE ship bridge/position', expect: '4103' },
  { id: 'T3', test: 'Node writes another node', who: 'CEngine', request: 'CREATE cin · ship cargo/climate', expect: '4103' },
  { id: 'T4', test: 'Node sends a command', who: 'CEngine', request: 'CREATE cin · ship bridge/command', expect: '4103' },
  { id: 'T5', test: 'Ship deletes a shore record', who: 'CAnchorShip', request: 'DELETE · shore anchor/events/<record>', expect: '4103' },
  { id: 'T6', test: 'Coast updates the shore receipts container', who: 'CAnchorEdge', request: 'UPDATE · shore anchor/receipts', expect: '4103' },
  { id: 'T7', test: 'Control API without the token', who: 'any client', request: 'GET :8790/status', expect: '401' },
  { id: 'T8', test: 'Control: dashboard reads the shore journal', who: 'CAnchorDashboard', request: 'RETRIEVE shore anchor/events', expect: '2000' },
  { id: 'T9', test: 'Dashboard forges a journal record', who: 'CAnchorDashboard', request: 'CREATE cin · shore anchor/events', expect: '4103' },
  { id: 'T10', test: 'Dashboard subscribes without create permission', who: 'CAnchorDashboard', request: 'CREATE sub · shore anchor/receipts', expect: '4103' },
  { id: 'T11', test: 'MQTTS without a client certificate', who: 'unverified client', request: 'TLS connect :8883', expect: 'TLS rejected' },
  { id: 'T12', test: 'Control: dashboard subscribes to events', who: 'CAnchorDashboard', request: 'CREATE sub · shore anchor/events', expect: '2001' },
  { id: 'T13', test: 'Device forges its own command', who: 'CBridge', request: 'CREATE cin · ship bridge/command', expect: '4103' },
  { id: 'T14', test: 'Read-only token attempts a command', who: 'status token', request: 'POST :8790/commands', expect: '403' },
  { id: 'T15', test: 'Application changes its own policy', who: 'CAnchorShore', request: 'UPDATE · shore acp-anchor-events', expect: '4103' },
]

export const TRANSPORT: { hop: string; how: string; open?: boolean }[] = [
  { hop: 'Ship DDS publisher → coastal IPE (UDP)', how: 'Native Cyclone DDS, six configurable QoS policies; defaults bind to loopback. DDS Security is not enabled: use an isolated trusted link or an authenticated encrypted tunnel. CSE ACP protects only the HTTPS side.', open: true },
  { hop: 'Control API (HTTPS 8790)', how: 'Operator token and a separate status-only token; the latter permits GET /status counters and excludes raw events, location and diagnostics' },
  { hop: 'CSE ↔ CSE, core → CSE (HTTPS)', how: 'CA and hostname verification enabled; binding the authenticated caller to its Originator remains open', open: true },
  { hop: 'ESP32 → ship CSE (MQTTS 8883)', how: 'Client certificates required, allow_anonymous=false; binding certificate identity to topics and Originator remains open', open: true },
  { hop: 'Position and AI access', how: 'Ship dashboard cannot read position; shore dashboard can read location-containing events. AI has no journal grant. Do not pass dashboard credentials or raw events to an LLM.', open: true },
]
