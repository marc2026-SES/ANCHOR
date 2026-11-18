// ACPs from the ACP design doc: what `anchor ... bootstrap` (src/anchor/resources.py) provisions,
// renamed acp-<resource path> (one ACP per resource), plus the grants marked ★.
// Every ACP's self-privileges (pvs) go to CAdmin only.
// Ops: C create(1), R retrieve(2), U update(4), D delete(8), N notify(16), Di discover(32), all = 63

export interface Grant { who: string; ops: string }
export interface AcpRow { name: string; on: string; cse: string; grants: Grant[] }

export const ACPS: AcpRow[] = [
  { name: 'acp-ddsIpe', on: 'native DDS IPE AE (edge option)', cse: 'coast',
    grants: [{ who: 'CAnchorDDSIPE, CAnchorEdge, CAnchorDashboard', ops: 'R Di' }] },
  { name: 'acp-ddsIpe-vibration, acp-ddsIpe-status', on: 'raw vibration windows and DDS status; no command or main-journal access', cse: 'coast',
    grants: [{ who: 'CAnchorDDSIPE', ops: 'R Di C(CIN)' }, { who: 'CAnchorEdge, CAnchorDashboard', ops: 'R Di' }] },
  { name: 'acp-anchor, acp-anchor-dispatches, -quarantine, -ruleState, -audit', on: 'anchor (core AE) and internal journals', cse: 'ship, coast, shore',
    grants: [{ who: 'core of that site', ops: 'all' }] },
  { name: 'acp-anchor-events', on: 'anchor/events', cse: 'ship',
    grants: [{ who: 'CAnchorShip', ops: 'all' }] },
  { name: 'acp-anchor-events', on: 'anchor/events', cse: 'coast',
    grants: [{ who: 'CAnchorEdge', ops: 'all' }, { who: 'CAnchorShip', ops: 'C R Di' }, { who: 'CAnchorDashboard (dashboard_edge_read)', ops: 'R Di' }] },
  { name: 'acp-anchor-events', on: 'anchor/events', cse: 'shore',
    grants: [{ who: 'CAnchorShore', ops: 'all' }, { who: 'CAnchorEdge, CAnchorShip', ops: 'C R Di' }, { who: 'CAnchorDashboard', ops: 'R Di C(SUB)' }] },
  { name: 'acp-anchor-receipts, acp-anchor-commands', on: 'anchor/receipts, anchor/commands', cse: 'coast',
    grants: [{ who: 'CAnchorEdge', ops: 'all' }, { who: 'CAnchorShip', ops: 'R Di' }] },
  { name: 'acp-anchor-commands', on: 'anchor/commands (optional monitor read)', cse: 'coast',
    grants: [{ who: 'CAnchorDashboard (dashboard_edge_read)', ops: 'R Di' }] },
  { name: 'acp-anchor-receipts, acp-anchor-commands', on: 'anchor/receipts, anchor/commands', cse: 'shore',
    grants: [{ who: 'CAnchorShore', ops: 'all' }, { who: 'CAnchorEdge, CAnchorShip', ops: 'R Di' }, { who: '★ CAnchorDashboard', ops: 'R Di' }] },
  { name: 'acp-shipCommands, acp-edgeCommands', on: 'poller AE + pollingChannel (coast, shore)', cse: 'coast, shore',
    grants: [{ who: 'core, CAnchorShipPoll / CAnchorEdgePoll', ops: 'all' }] },
  { name: 'acp-<node>, acp-<node>-<sensor>, acp-<node>-commandResults', on: 'bridge, engine, cargo AEs and their sensor containers', cse: 'ship',
    grants: [{ who: 'CAnchorShip', ops: 'all' }, { who: 'CBridge, CEngine, CCargo (own node)', ops: 'C R U' }, { who: 'CAnchorDashboard', ops: 'R' }] },
  { name: 'acp-bridge-position', on: 'bridge/position', cse: 'ship',
    grants: [{ who: 'CAnchorShip', ops: 'all' }, { who: 'CBridge', ops: 'C R U' }, { who: 'CAnchorAgent', ops: 'R Di' }] },
  { name: 'acp-agent, acp-bridge, acp-engine, acp-cargo', on: 'AI identity AE and sensor parent AEs (additional reader grant)', cse: 'ship',
    grants: [{ who: 'CAnchorAgent', ops: 'R Di' }] },
  { name: 'acp-bridge-attitude, acp-engine-vibration, acp-engine-gas, acp-cargo-climate, acp-cargo-hatch', on: 'Five other sensor CNTs (additional reader grant); excludes commandResults and SDT', cse: 'ship',
    grants: [{ who: 'CAnchorAgent', ops: 'R Di' }] },
  { name: 'acp-bridge-command, acp-engine-command', on: '<node>/command (reportingGroup members)', cse: 'ship',
    grants: [{ who: 'CAnchorShip', ops: 'all' }, { who: 'CBridge, CEngine (own node)', ops: 'R C(SUB)' }] },
  { name: 'acp-anchor-reportingGroup', on: 'anchor/reportingGroup', cse: 'ship',
    grants: [{ who: 'CAnchorShip', ops: 'all' }] },
  { name: '★ acp-anchorAI (proposed)', on: 'anchorAI AE, aiStatus, anomaly, summary, prognosis, linkForecast', cse: 'ship, shore (copy)',
    grants: [{ who: '★ CAnchorAgent (the AI writer; not CAdmin)', ops: 'C R' },
      { who: '★ CAnchorDashboard', ops: 'R Di' }] },
  { name: '★ acp-fleetAI (proposed)', on: 'fleetAI AE, fleetStatus, conflicts', cse: 'coast (MEC)',
    grants: [{ who: '★ fleet-AI writer (own originator, not CAdmin)', ops: 'C R' },
      { who: '★ CAnchorDashboard', ops: 'R Di' }] },
]

/** Denied-request tests. server/deny_tests.py runs T1–T10 against the CSEs and writes deny-results.json;
 *  the rest stay "not run" until executed on the full stack. */
export interface DenyResult { id: string; expected: number; got: number | null; status: 'pass' | 'fail' | 'error' | 'skipped'; note: string; at: string }
export interface DenyRun { ranAt: string | null; cses: Record<string, string>; results: DenyResult[] }

export const DENY_TESTS: { id: string; test: string; who: string; request: string; expect: string }[] = [
  { id: 'T1', test: 'Dashboard writes sensor data', who: 'CAnchorDashboard', request: 'CREATE cin, ship bridge/attitude', expect: '4103' },
  { id: 'T2', test: 'Dashboard reads the position', who: 'CAnchorDashboard', request: 'RETRIEVE ship bridge/position', expect: '4103' },
  { id: 'T3', test: 'Node writes another node', who: 'CEngine', request: 'CREATE cin, ship cargo/climate', expect: '4103' },
  { id: 'T4', test: 'Node sends a command', who: 'CEngine', request: 'CREATE cin, ship bridge/command', expect: '4103' },
  { id: 'T5', test: 'Ship deletes a shore record', who: 'CAnchorShip', request: 'DELETE, shore anchor/events/<record>', expect: '4103' },
  { id: 'T6', test: 'Coast updates the shore receipts container', who: 'CAnchorEdge', request: 'UPDATE, shore anchor/receipts', expect: '4103' },
  { id: 'T7', test: 'Control API without the token', who: 'any client', request: 'GET :8790/status', expect: '401' },
  { id: 'T8', test: 'Control: dashboard reads the shore journal', who: 'CAnchorDashboard', request: 'RETRIEVE shore anchor/events', expect: '2000' },
  { id: 'T9', test: 'Dashboard forges a journal record', who: 'CAnchorDashboard', request: 'CREATE cin, shore anchor/events', expect: '4103' },
  { id: 'T10', test: 'Dashboard subscribes without create permission', who: 'CAnchorDashboard', request: 'CREATE sub, shore anchor/receipts', expect: '4103' },
  { id: 'T11', test: 'MQTTS without a client certificate', who: 'unverified client', request: 'TLS connect :8883', expect: 'TLS rejected' },
  { id: 'T12', test: 'Control: dashboard subscribes to events', who: 'CAnchorDashboard', request: 'CREATE sub, shore anchor/events', expect: '2001' },
  { id: 'T13', test: 'Device forges its own command', who: 'CBridge', request: 'CREATE cin, ship bridge/command', expect: '4103' },
  { id: 'T14', test: 'Read-only token attempts a command', who: 'status token', request: 'POST :8790/commands', expect: '403' },
  { id: 'T15', test: 'Application changes its own policy', who: 'CAnchorShore', request: 'UPDATE, shore acp-anchor-events', expect: '4103' },
  { id: 'T16', test: 'Control: AI reads GPS, one of six authorized sensors', who: 'CAnchorAgent', request: 'RETRIEVE ship bridge/position/la', expect: '2000' },
  { id: 'T17', test: 'AI forges a sensor record', who: 'CAnchorAgent', request: 'CREATE cin, ship bridge/attitude', expect: '4103' },
  { id: 'T18', test: 'AI subscribes without create permission', who: 'CAnchorAgent', request: 'CREATE sub, ship engine/vibration', expect: '4103' },
  { id: 'T19', test: 'AI reads a command container', who: 'CAnchorAgent', request: 'RETRIEVE ship bridge/command', expect: '4103' },
]

export const TRANSPORT: { hop: string; how: string; open?: boolean }[] = [
  { hop: 'Ship DDS publisher → coastal IPE (UDP)', how: 'Native Cyclone DDS, six configurable QoS policies; defaults bind to loopback. DDS Security is not enabled: use an isolated trusted link or an authenticated encrypted tunnel. CSE ACP protects only the HTTPS side.', open: true },
  { hop: 'Control API (HTTPS 8790)', how: 'Operator token and a separate status-only token; the latter permits GET /status counters and excludes raw events, location and diagnostics' },
  { hop: 'CSE ↔ CSE, core → CSE (HTTPS)', how: 'CA and hostname verification enabled; binding the authenticated caller to its Originator remains open', open: true },
  { hop: 'ESP32 → ship CSE (MQTTS 8883)', how: 'Client certificates required, allow_anonymous=false; binding certificate identity to topics and Originator remains open', open: true },
  { hop: 'Position and AI access', how: 'CAnchorAgent can retrieve and discover six ship sensors, including GPS position. No writes, subscriptions, commands or journal grants. Ship dashboard still cannot read position; shore dashboard can read location-containing events. Use the separate AI identity.' },
  { hop: 'AI results (anchorAI, fleetAI)', how: 'read by the Shore Monitor as CAnchorDashboard: the shore CSE copy of anchorAI first, else the ship CSE; fleetAI on the coastal CSE, while link A is down no new ship-AI results reach the shore (shown as "Ship not reachable", not an AI failure), linkForecast lat/lon stripped before storing', open: true },
  { hop: 'Shore Monitor API (HTTPS 8795)', how: 'loopback by default, beyond loopback: Bearer token (never in a URL or browser storage), or explicit "open": true for a private team network (Tailscale) trusted end to end' },
  { hop: 'Core tokens on the monitor', how: 'ship and edge only, read-only /status (file tokens, or the tokenless read-only relay on the MEC map server), the shore core token is refused: it can issue commands', open: true },
]
