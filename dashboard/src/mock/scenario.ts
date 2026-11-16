// Hard-coded demo run in the ANCHOR core formats (envelope schemaVersion 2).
// It follows the shooting order in the team README: link A (ship ↔ coast) and link B (coast ↔ shore)
// are cut at different times and are never needed together. Deterministic (seeded).
// Replace with the Shore Monitor AE feed once it reads the real journals.

import type { AnchorEvent, Command, Delivered, Device, FeedItem, LinkChange, LinkId, Run, Sensor } from '../types'

export const START = Date.parse('2026-11-18T05:00:00.000Z')
const S = 1_000
const MIN = 60_000
export const DURATION = 20 * MIN
const VESSEL = 'ship-001'
const SCAN = 2_000   // scan_seconds (deploy/*.json)
const BATCH = 40     // batch_size
const BURST = 4      // critical_burst: 4 critical, then 1 ordinary

/** link down windows, minutes from START */
const DOWN: Record<LinkId, [number, number][]> = {
  A: [[4, 9], [11, 14]],
  B: [[6, 11.5]],
}
export const isUp = (link: LinkId, ms: number) =>
  !DOWN[link].some(([a, b]) => ms >= START + a * MIN && ms < START + b * MIN)

const COMMAND_AT = START + 5 * MIN
const COMMAND_TTL = 5 * MIN
const COMMAND_INTERVAL = 200
const COMMAND_DURATION = 30 * S
const TILT = { from: START + 7 * MIN, to: START + 7 * MIN + 20 * S }
const ACK_AT = START + 9 * MIN + 40 * S
const ENGINE_REBOOT = { down: START + 16 * MIN, up: START + 16 * MIN + 6 * S }
const HATCH_CHANGES = [START + 3 * MIN + 10 * S, START + 3 * MIN + 40 * S, START + 8 * MIN, START + 8 * MIN + 30 * S]

function rng(seed: number) {
  return () => {
    seed |= 0; seed = (seed + 0x6d2b79f5) | 0
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}
const r2 = (v: number) => Math.round(v * 100) / 100

export function linkChanges(): LinkChange[] {
  const out: LinkChange[] = [{ t: START, link: 'A', up: true }, { t: START, link: 'B', up: true }]
  for (const link of ['A', 'B'] as LinkId[]) {
    for (const [a, b] of DOWN[link]) {
      out.push({ t: START + a * MIN, link, up: false }, { t: START + b * MIN, link, up: true })
    }
  }
  return out.sort((x, y) => x.t - y.t || x.link.localeCompare(y.link))
}

// ---------------------------------------------------------------- generation (ship side)

interface Sample { t: number; sensor: Sensor; payload: Record<string, unknown>; quality?: AnchorEvent['quality'] }

function tiltRoll(t: number): number | null {
  if (t < TILT.from || t >= TILT.to + 6 * S) return null
  const s = (t - TILT.from) / S
  if (s < 2) return 6 + s * 12          // heeling over
  if (s < 16) return 30 + Math.sin(s) * 1.5
  if (s < 19) return 30 - (s - 16) * 7  // back up
  return 4
}

function bridge(rand: () => number, appliedAt: number | null): Sample[] {
  const out: Sample[] = []
  const fast = (t: number) => appliedAt !== null && t >= appliedAt && t < appliedAt + COMMAND_DURATION
  for (let t = START; t < START + DURATION;) {
    const tilt = tiltRoll(t)
    const roll = tilt ?? 3 * Math.sin((2 * Math.PI * (t - START)) / 9_000) + (rand() - 0.5) * 1.2
    const pitch = 1.5 * Math.sin((2 * Math.PI * (t - START)) / 13_000) + (rand() - 0.5) * 0.6
    out.push({ t, sensor: 'attitude', payload: { roll: r2(roll), pitch: r2(pitch), ok: true,
      reportIntervalMs: fast(t) ? COMMAND_INTERVAL : 1000 } })
    t += fast(t) ? COMMAND_INTERVAL : 1000
  }
  for (let t = START + 500; t < START + DURATION; t += 2_000) {
    out.push({ t, sensor: 'position', quality: { validFix: true },
      payload: { gps: 'ok', fix: true, lat: 37.5503 + (rand() - 0.5) * 0.0002, lon: 127.0731 + (rand() - 0.5) * 0.0002,
        sats: 8, hdop: 1.1, sog_kn: r2(0.3 + rand() * 0.2), cog: Math.round(rand() * 360) } })
  }
  return out
}

function engine(rand: () => number): Sample[][] {
  // two boots: the node reboots at 16:00 and comes back 6 s later with a new bootId
  const boots: Sample[][] = [[], []]
  const bootStart = [START - 10 * MIN, ENGINE_REBOOT.up]
  for (let t = START; t < START + DURATION; t += 1000) {
    if (t >= ENGINE_REBOOT.down && t < ENGINE_REBOOT.up) continue
    const b = t < ENGINE_REBOOT.down ? 0 : 1
    boots[b].push({ t, sensor: 'vibration', payload: { vib_rms_g: Number((0.05 + (rand() - 0.5) * 0.02).toFixed(4)),
      vib_peak_g: r2(0.2 + rand() * 0.05), mean_g: r2(1 + (rand() - 0.5) * 0.02) } })
  }
  for (let t = START + 250; t < START + DURATION; t += 2_000) {
    if (t >= ENGINE_REBOOT.down && t < ENGINE_REBOOT.up) continue
    const b = t < ENGINE_REBOOT.down ? 0 : 1
    const warm = t - bootStart[b] > 120_000
    boots[b].push({ t, sensor: 'gas', quality: { warmed: warm },
      payload: { mq2_v: Number(((warm ? 0.42 : 0.9 - ((t - bootStart[b]) / 120_000) * 0.45) + (rand() - 0.5) * 0.02).toFixed(3)), warm } })
  }
  return boots
}

function cargo(rand: () => number): Sample[] {
  const out: Sample[] = []
  for (let t = START + 750; t < START + DURATION; t += 5_000) {
    const ok = rand() > 0.02
    out.push({ t, sensor: 'climate', quality: { sensorOk: ok },
      payload: ok ? { ok, temp_c: r2(21.5 + Math.sin((t - START) / 400_000) * 0.4 + (rand() - 0.5) * 0.2), rh: Math.round(48 + (rand() - 0.5) * 3) } : { ok } })
  }
  const stateAt = (t: number) => (HATCH_CHANGES.filter(c => c <= t).length % 2 ? 'open' : 'closed')
  const openSince = (t: number) => HATCH_CHANGES.filter((c, i) => c <= t && i % 2 === 0).pop() ?? t
  for (let t = START + 1_000; t < START + DURATION; t += 30_000) {
    const st = stateAt(t)
    out.push({ t, sensor: 'hatch', payload: { state: st, event: 'periodic', open_s: st === 'open' ? Math.round((t - openSince(t)) / S) : 0 } })
  }
  HATCH_CHANGES.forEach((t, i) => out.push({ t, sensor: 'hatch', payload: { state: i % 2 ? 'closed' : 'open', event: 'change', open_s: i % 2 ? 0 : 0 } }))
  return out
}

function envelope(device: Device, bootId: string, seq: number, s: Sample): AnchorEvent {
  return {
    eventId: `${VESSEL}:${device}:${bootId}:${seq}`, vesselId: VESSEL, deviceId: device, bootId, seq,
    generatedAt: s.t, kind: s.sensor === 'commandResults' ? 'commandResult' : 'measurement', sensor: s.sensor,
    priority: s.sensor === 'commandResults' ? 'critical' : 'normal',
    quality: { identity: 'device-boot-sequence', time: 'device', droppedBefore: 0, ...s.quality },
    payload: { ...s.payload, bootId, seq, ts: s.t },
  }
}

/** number one device's samples in time order with one shared seq counter per boot */
function number(device: Device, bootId: string, samples: Sample[]): AnchorEvent[] {
  return [...samples].sort((a, b) => a.t - b.t).map((s, i) => envelope(device, bootId, i, s))
}

/** tilt rule (src/anchor/incidents.py): ≥ 25° for 2 s opens, ≤ 15° for 2 s recovers */
function tiltIncidents(attitude: AnchorEvent[]): AnchorEvent[] {
  const out: AnchorEvent[] = []
  let cand: AnchorEvent | null = null, rec: AnchorEvent | null = null, active: string | null = null
  for (const e of attitude) {
    const angle = Math.max(Math.abs(e.payload.roll as number), Math.abs(e.payload.pitch as number))
    if (!active) {
      if (angle < 25) { cand = null; continue }
      cand = cand ?? e
      if (e.generatedAt - cand.generatedAt < 2_000) continue
      active = 'tilt-' + (cand.seq ?? 0).toString(16).padStart(8, '0')
      out.push(incident(e, active, 'opened'))
    } else if (angle <= 15) {
      rec = rec ?? e
      if (e.generatedAt - rec.generatedAt >= 2_000) { out.push(incident(e, active, 'recovered')); active = cand = rec = null }
    } else rec = null
  }
  return out
}

function incident(e: AnchorEvent, id: string, status: 'opened' | 'recovered'): AnchorEvent {
  return { eventId: `${id}:${status}`, vesselId: VESSEL, deviceId: 'ship-rule', bootId: e.bootId, seq: e.seq,
    generatedAt: e.generatedAt, kind: 'incident', priority: 'critical',
    payload: { incidentId: id, status, rule: 'tilt-duration-v1', evidenceEventId: e.eventId, roll: e.payload.roll, pitch: e.payload.pitch } }
}

// ---------------------------------------------------------------- transfer (one hop, core transfer_tick)

interface Item { id: string; ready: number; gen: number; critical: boolean }

/** Every SCAN ms while the link is up, the sender pushes up to BATCH pending items, critical first (fair_batch). */
function hop(items: Item[], link: LinkId, offset: number, rand: () => number): Map<string, number> {
  const up = (t: number) => isUp(link, t)
  const arrived = new Map<string, number>()
  const pending = [...items].sort((a, b) => a.ready - b.ready)
  let next = 0
  const queue: Item[] = []
  for (let tick = START + offset; tick < START + DURATION + 5 * MIN; tick += SCAN) {
    while (next < pending.length && pending[next].ready <= tick) queue.push(pending[next++])
    if (!queue.length || !up(tick)) continue
    const urgent = queue.filter(i => i.critical).sort((a, b) => a.gen - b.gen)
    const normal = queue.filter(i => !i.critical).sort((a, b) => a.gen - b.gen)
    const batch: Item[] = []
    let ui = 0, ni = 0
    while (batch.length < BATCH && (ui < urgent.length || ni < normal.length)) {
      for (let k = 0; k < BURST && ui < urgent.length && batch.length < BATCH; k++) batch.push(urgent[ui++])
      if (ni < normal.length && batch.length < BATCH) batch.push(normal[ni++])
    }
    const base = tick + 80 + rand() * 250
    batch.forEach((i, k) => arrived.set(i.id, Math.round(base + k * 12)))
    const sent = new Set(batch.map(i => i.id))
    for (let k = queue.length - 1; k >= 0; k--) if (sent.has(queue[k].id)) queue.splice(k, 1)
  }
  return arrived
}

// ---------------------------------------------------------------- run

export function buildRun(mode: 'anchor' | 'baseline'): Run {
  const rand = rng(20261118)
  const net = rng(mode === 'anchor' ? 7 : 11)

  // command: ANCHOR caches it at the coast while B is up and hands it to the ship at the next A contact
  const cmd: Command = { commandId: 'demo-profile-01', target: 'reportingGroup', intervalMs: COMMAND_INTERVAL,
    durationMs: COMMAND_DURATION, issuedAt: COMMAND_AT, expiresAt: COMMAND_AT + COMMAND_TTL,
    cachedAtCoast: null, deliveredToShip: null, appliedAt: null }
  if (mode === 'anchor') {
    cmd.cachedAtCoast = firstTick(COMMAND_AT, 1_000, t => isUp('B', t))
    cmd.deliveredToShip = firstTick(cmd.cachedAtCoast!, 0, t => isUp('A', t))
  } else {
    // no coastal custody: the ship only sees the command while ship and shore are connected end to end
    cmd.deliveredToShip = firstTick(COMMAND_AT, 0, t => isUp('A', t) && isUp('B', t))
  }
  if (cmd.deliveredToShip !== null && cmd.deliveredToShip >= cmd.expiresAt) cmd.deliveredToShip = null
  if (cmd.deliveredToShip !== null) cmd.appliedAt = cmd.deliveredToShip + 1_400

  const bridgeSamples = bridge(rand, cmd.appliedAt)
  const [eng0, eng1] = engine(rand)
  if (cmd.appliedAt !== null) {
    for (const list of [bridgeSamples, eng0]) {
      list.push({ t: cmd.appliedAt + (list === eng0 ? 180 : 0), sensor: 'commandResults', payload: {
        driver: 'esp32', commandId: cmd.commandId, status: 'applied', appliedAtMs: cmd.appliedAt,
        appliedIntervalMs: COMMAND_INTERVAL, profileUntilMs: cmd.appliedAt + COMMAND_DURATION } })
    }
  }
  const bridgeEvents = number('bridge', 'b7f3a1c25e9d0a14', bridgeSamples)
  const events: AnchorEvent[] = [
    ...bridgeEvents,
    ...number('engine', '4c2e8f0a913b7d6e', eng0),
    ...number('engine', 'e01d59a7c3f2b846', eng1),
    ...number('cargo', '9a6b3e1f0c7d2584', cargo(rand)),
    ...tiltIncidents(bridgeEvents.filter(e => e.sensor === 'attitude')),
  ]
  const opened = events.find(e => e.kind === 'incident' && e.payload.status === 'opened')

  // ---- delivery
  const coastAt = new Map<string, number>()
  const shoreAt = new Map<string, number>()
  if (mode === 'anchor') {
    const shipItems = events.map(e => ({ id: e.eventId, ready: e.generatedAt, gen: e.generatedAt, critical: e.priority === 'critical' }))
    hop(shipItems, 'A', 0, net).forEach((v, k) => coastAt.set(k, v))
    // the coast operator acknowledges the incident once it has arrived there
    if (opened && coastAt.has(opened.eventId)) {
      const at = Math.max(ACK_AT, coastAt.get(opened.eventId)! + 5 * S)
      const ack: AnchorEvent = { eventId: `${opened.payload.incidentId}:ack:edge`, vesselId: VESSEL, deviceId: 'edge-operator',
        bootId: 'operator', seq: null, generatedAt: at, kind: 'incidentAck', priority: 'critical',
        payload: { incidentId: opened.payload.incidentId, operator: 'coast duty officer', site: 'edge' } }
      events.push(ack)
      coastAt.set(ack.eventId, at)
    }
    const coastItems = events.filter(e => coastAt.has(e.eventId))
      .map(e => ({ id: e.eventId, ready: coastAt.get(e.eventId)!, gen: e.generatedAt, critical: e.priority === 'critical' }))
    hop(coastItems, 'B', 1_000, net).forEach((v, k) => shoreAt.set(k, v))
  } else {
    // baseline: forward live only, no custody anywhere; whatever is generated while the path is down is dropped
    // (sent at the next scan only if ship and shore are connected end to end at that moment)
    for (const e of events) {
      const tick = Math.ceil((e.generatedAt - START) / SCAN) * SCAN + START
      if (isUp('A', tick) && isUp('B', tick)) shoreAt.set(e.eventId, Math.round(tick + 480 + net() * 250))
    }
  }

  const delivered: Delivered[] = events
    .map(e => ({ ...e, coastAt: coastAt.get(e.eventId) ?? null, shoreAt: shoreAt.get(e.eventId) ?? null }))
    .sort((a, b) => a.generatedAt - b.generatedAt)

  return {
    runId: `demo-${mode}-01`, mode, start: START, end: START + DURATION,
    events: delivered, links: linkChanges(), commands: [cmd], feed: feed(mode, delivered, cmd),
  }
}

function firstTick(from: number, offset: number, ok: (t: number) => boolean): number | null {
  let t = Math.ceil((from - START - offset) / SCAN) * SCAN + START + offset
  for (; t < START + DURATION + 5 * MIN; t += SCAN) if (ok(t)) return t + 300
  return null
}

function feed(mode: 'anchor' | 'baseline', events: Delivered[], cmd: Command): FeedItem[] {
  const f: FeedItem[] = []
  const at = (m: number) => START + m * MIN
  const shore = (pred: (e: Delivered) => boolean) => events.find(pred)?.shoreAt ?? undefined
  f.push({ t: at(0), kind: 'link', text: 'Links A (ship ↔ coast) and B (coast ↔ shore) up' })
  f.push({ t: at(4), kind: 'link', text: mode === 'anchor' ? 'Link A down · the ship keeps its originals' : 'Link A down · ship data is no longer forwarded' })
  f.push({ t: cmd.issuedAt, kind: 'command', text: `Shore issued ${cmd.commandId}: reportingGroup → ${cmd.intervalMs} ms for ${cmd.durationMs / 1000} s, valid ${(cmd.expiresAt - cmd.issuedAt) / 60_000} min` })
  if (cmd.cachedAtCoast) f.push({ t: cmd.cachedAtCoast, kind: 'command', text: 'Command stored at the coast (waits there for the ship)' })
  f.push({ t: at(6), kind: 'link', text: 'Link B down' })
  for (const e of events.filter(e => e.kind === 'incident')) {
    f.push({ t: e.generatedAt, seen: e.shoreAt ?? undefined, kind: 'incident',
      text: e.payload.status === 'opened' ? `Tilt incident opened on the ship (roll ${e.payload.roll}°, ≥ 25° for 2 s)` + (e.shoreAt ? '' : ' · never reached shore')
        : 'Tilt incident recovered (≤ 15° for 2 s)' + (e.shoreAt ? '' : ' · never reached shore') })
  }
  f.push({ t: at(9), kind: 'link', text: mode === 'anchor' ? 'Link A up · ship backlog to the coast, critical first' : 'Link A up · B still down, nothing reaches shore' })
  if (cmd.appliedAt) {
    f.push({ t: cmd.deliveredToShip!, kind: 'command', text: 'Ship picked the command up from the coast (pollingChannel)' })
    f.push({ t: cmd.appliedAt, seen: shore(e => e.kind === 'commandResult') , kind: 'command', text: 'Applied on BRIDGE and ENGINE (ESP32 results)' })
  } else {
    f.push({ t: cmd.expiresAt, kind: 'command', text: 'Command expired before any path reached the ship · not applied' })
  }
  const ack = events.find(e => e.kind === 'incidentAck')
  if (ack) f.push({ t: ack.generatedAt, seen: ack.shoreAt ?? undefined, kind: 'incident', text: `Coast operator acknowledged the tilt incident` })
  f.push({ t: at(11), kind: 'link', text: 'Link A down' })
  f.push({ t: at(11.5), kind: 'link', text: mode === 'anchor' ? 'Link B up · coast forwards what it holds while the ship is away' : 'Link B up · A still down' })
  if (mode === 'anchor') {
    const held = events.filter(e => e.coastAt !== null && e.coastAt < at(11.5) && e.shoreAt !== null && e.shoreAt >= at(11.5))
    const last = held.reduce((m, e) => Math.max(m, e.shoreAt!), 0)
    if (last) f.push({ t: last, kind: 'delivery', text: `Shore confirmed ${held.length.toLocaleString('en-US')} records held at the coast` })
  }
  f.push({ t: at(14), kind: 'link', text: mode === 'anchor' ? 'Link A up · rest of the ship backlog flows, final receipts go back to the ship' : 'Links A and B up again' })
  const reboot = events.find(e => e.deviceId === 'engine' && e.bootId === 'e01d59a7c3f2b846')
  if (reboot) f.push({ t: reboot.generatedAt, seen: reboot.shoreAt ?? undefined, kind: 'delivery',
    text: 'ENGINE rebooted · new bootId, seq restarts at 0 (not counted as loss)' })
  f.push({ t: at(2), kind: 'security', text: 'Denied 4103: CAnchorDashboard tried to write bridge/attitude (sample)' })
  f.push({ t: at(17), kind: 'security', text: 'Denied 401: control API called without the token (sample)' })
  return f
}
