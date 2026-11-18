// ACPs from the ACP design doc: what `anchor ... bootstrap` (src/anchor/resources.py) provisions,
// renamed acp-<resource path> (one ACP per resource), plus the grants marked ★.
// Every ACP's self-privileges (pvs) go to CAdmin only.
// Ops: C create(1) · R retrieve(2) · U update(4) · D delete(8) · N notify(16) · Di discover(32) · all = 63

export interface Grant { who: string; ops: string }
export interface AcpRow { name: string; on: string; cse: string; grants: Grant[] }

export const ACPS: AcpRow[] = [
  { name: 'acp-anchor · acp-anchor-dispatches · -quarantine · -ruleState · -audit', on: 'anchor (core AE) and internal journals', cse: 'ship · coast · shore',
    grants: [{ who: 'core of that site', ops: 'all' }] },
  { name: 'acp-anchor-events', on: 'anchor/events', cse: 'ship',
    grants: [{ who: 'CAnchorShip', ops: 'all' }] },
  { name: 'acp-anchor-events', on: 'anchor/events', cse: 'coast',
    grants: [{ who: 'CAnchorEdge', ops: 'all' }, { who: 'CAnchorShip', ops: 'C R Di' }, { who: '★ CAnchorDashboard (optional: coastAt)', ops: 'R Di' }] },
  { name: 'acp-anchor-events', on: 'anchor/events', cse: 'shore',
    grants: [{ who: 'CAnchorShore', ops: 'all' }, { who: 'CAnchorEdge · CAnchorShip', ops: 'C R Di' }, { who: '★ CAnchorDashboard', ops: 'R Di' },
      { who: '★ CAnchorDashboard (subscriptions only: acod chty [23])', ops: 'C' }] },
  { name: 'acp-anchor-receipts · acp-anchor-commands', on: 'anchor/receipts · anchor/commands', cse: 'coast',
    grants: [{ who: 'CAnchorEdge', ops: 'all' }, { who: 'CAnchorShip', ops: 'R Di' }, { who: '★ CAnchorDashboard (optional, commands only)', ops: 'R Di' }] },
  { name: 'acp-anchor-receipts · acp-anchor-commands', on: 'anchor/receipts · anchor/commands', cse: 'shore',
    grants: [{ who: 'CAnchorShore', ops: 'all' }, { who: 'CAnchorEdge · CAnchorShip', ops: 'R Di' }, { who: '★ CAnchorDashboard', ops: 'R Di' }] },
  { name: 'acp-shipCommands · acp-edgeCommands', on: 'poller AE + pollingChannel (coast · shore)', cse: 'coast · shore',
    grants: [{ who: 'core · CAnchorShipPoll / CAnchorEdgePoll', ops: 'all' }] },
  { name: 'acp-<node> · acp-<node>-<sensor> · acp-<node>-commandResults', on: 'bridge · engine · cargo AEs and their sensor containers', cse: 'ship',
    grants: [{ who: 'CAnchorShip', ops: 'all' }, { who: 'CBridge · CEngine · CCargo (own node)', ops: 'C R U' }, { who: 'CAnchorDashboard', ops: 'R' }] },
  { name: 'acp-bridge-position', on: 'bridge/position', cse: 'ship',
    grants: [{ who: 'CAnchorShip', ops: 'all' }, { who: 'CBridge', ops: 'C R U' }] },
  { name: 'acp-bridge-command · acp-engine-command', on: '<node>/command (reportingGroup members)', cse: 'ship',
    grants: [{ who: 'CAnchorShip', ops: 'all' }, { who: 'CBridge · CEngine (own node)', ops: 'C R' }] },
  { name: '★ acp-anchorAI (proposed)', on: 'anchorAI AE · aiStatus, anomaly, summary, prognosis, linkForecast', cse: 'ship · shore (copy)',
    grants: [{ who: '★ CAnchorAgent (the AI writer; not CAdmin)', ops: 'C R' },
      { who: '★ CAnchorDashboard', ops: 'R Di' }] },
  { name: '★ acp-fleetAI (proposed)', on: 'fleetAI AE · fleetStatus, conflicts', cse: 'coast (MEC)',
    grants: [{ who: '★ fleet-AI writer (own originator, not CAdmin)', ops: 'C R' },
      { who: '★ CAnchorDashboard', ops: 'R Di' }] },
]

/** Denied-request tests. server/deny_tests.py runs them against the CSEs and writes deny-results.json. */
export interface DenyResult { id: string; expected: number; got: number | null; status: 'pass' | 'fail' | 'error' | 'skipped'; note: string; at: string }
export interface DenyRun { ranAt: string | null; cses: Record<string, string>; results: DenyResult[] }

export const DENY_TESTS: { id: string; test: string; who: string; request: string; expect: string }[] = [
  { id: 'T1', test: 'Dashboard writes sensor data', who: 'CAnchorDashboard', request: 'CREATE cin · ship bridge/attitude', expect: '4103' },
  { id: 'T2', test: 'Dashboard reads the position', who: 'CAnchorDashboard', request: 'RETRIEVE ship bridge/position', expect: '4103' },
  { id: 'T3', test: 'Node writes another node', who: 'CEngine', request: 'CREATE cin · ship cargo/climate', expect: '4103' },
  { id: 'T4', test: 'Node sends a command', who: 'CEngine', request: 'CREATE cin · ship bridge/command', expect: '4103' },
  { id: 'T5', test: 'Ship deletes a shore record', who: 'CAnchorShip', request: 'DELETE · shore anchor/events/<record>', expect: '4103' },
  { id: 'T6', test: 'Coast changes the shore receipts journal', who: 'CAnchorEdge', request: 'UPDATE · shore anchor/receipts', expect: '4103' },
  { id: 'T7', test: 'Control API without the token', who: 'any client', request: 'GET :8790/status', expect: '401' },
  { id: 'T8', test: 'Control: dashboard reads the shore journal', who: 'CAnchorDashboard', request: 'RETRIEVE shore anchor/events', expect: '2000' },
  { id: 'T9', test: 'Dashboard writes into the shore journal', who: 'CAnchorDashboard', request: 'CREATE cin · shore anchor/events', expect: '4103' },
  { id: 'T10', test: 'Dashboard subscribes outside anchor/events', who: 'CAnchorDashboard', request: 'CREATE sub · shore anchor/receipts', expect: '4103' },
]

export const TRANSPORT: { hop: string; how: string; open?: boolean }[] = [
  { hop: 'Control API (port 8790)', how: 'Bearer token from init-token, compared in constant time; never put in a URL or browser storage' },
  { hop: 'CSE ↔ CSE, core → CSE (HTTP)', how: 'trusted LAN / VPN in the demo; HTTPS for untrusted networks (the core refuses to switch off CA verification)' },
  { hop: 'ESP32 → ship CSE (MQTT)', how: 'Mosquitto allow_anonymous on the boat LAN (dev) · MQTT accounts and TLS still to add', open: true },
  { hop: 'Ship position (privacy)', how: 'bridge/position has its own ACP without the dashboard identity, but position events are copied into anchor/events, which the dashboard reads · the Shore Monitor strips lat/lon before storing · the control API still shows the latest value to token holders', open: true },
  { hop: 'AI results (anchorAI)', how: 'read by the Shore Monitor as CAnchorDashboard over https, never as CAdmin: the shore CSE copy first, else the ship CSE · while link A is down no new results reach the shore (shown as "Ship not reachable", not an AI failure) · linkForecast lat/lon stripped before storing · the copy\'s route and writer identity still to agree', open: true },
  { hop: 'Shore Monitor API (port 8795)', how: '127.0.0.1 only unless a token file is set · the dashboard /api proxy forwards GET only, so runs are started and closed on the monitor machine' },
  { hop: 'Core tokens on the monitor', how: 'ship and edge only, read from files, for /status · the shore core token (it can issue commands) is refused · the same tokens can also acknowledge incidents: a read-only status token is still to add', open: true },
]
