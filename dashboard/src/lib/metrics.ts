// Metric definitions (design doc "측정 지표 정의", adapted to the ANCHOR core).
// Inputs: the events with the CSE creation time of their coast and shore copies, and the link changes.
// The same code runs on the mock and on live data from the Shore Monitor AE.

import type { Command, Delivered, LinkChange, LinkId } from '../types'

/** delivered more than this after generation = it waited on the ship or at the coast (shore_server.py DELAYED_S) */
export const HELD_AFTER_MS = 10_000

export const isHeld = (e: Delivered) => e.shoreAt !== null && e.shoreAt - e.generatedAt > HELD_AFTER_MS
/** hardware streams whose seq can prove completeness (incidents reuse the evidence seq, so they are excluded) */
export const isCounted = (e: Delivered) => (e.kind === 'measurement' || e.kind === 'commandResult') && e.seq !== null
const stream = (e: Delivered) => `${e.deviceId}:${e.bootId}`

export function linkUpAt(links: LinkChange[], link: LinkId, ms: number): boolean {
  let up = false
  for (const c of links) {
    if (c.t > ms) break
    if (c.link === link) up = c.up
  }
  return up
}

function percentile(xs: number[], p: number): number | null {
  if (!xs.length) return null
  const s = [...xs].sort((a, b) => a - b)
  return s[Math.min(s.length - 1, Math.max(0, Math.ceil((p / 100) * s.length) - 1))]
}

export interface Stats { p50: number | null; p95: number | null; max: number | null; n: number }
export const stats = (xs: number[]): Stats => ({
  p50: percentile(xs, 50), p95: percentile(xs, 95), max: xs.length ? Math.max(...xs) : null, n: xs.length,
})

/** what happened after one link came back */
export interface Drain { link: LinkId; up: number; items: number; firstAfter: number | null; drained: number | null }

export interface Metrics {
  generated: number        // events generated on the ship up to now (live: ship status "accepted")
  coastCustody: number     // copies stored at the coast
  shoreConfirmed: number   // copies stored at the shore
  heldOnShip: number       // generated, not yet at the coast (and not dropped)
  waitingAtCoast: number   // at the coast, not yet at the shore
  lost: number             // never delivered (baseline drops); at the end of a run = seq gaps at shore
  seqGaps: number          // gaps in (device, bootId) seq ranges seen at shore right now
  streams: number          // (device, bootId) streams seen at shore
  reboots: number          // devices that showed more than one bootId
  duplicates: number
  live: Stats              // end-to-end delay of records that never waited
  hopA: Stats              // ship → coast, live
  hopB: Stats              // coast → shore, live
  held: Stats              // end-to-end delay of records that waited on the ship or at the coast
  heldCritical: Stats      // the same, critical only (incidents, command results, acknowledgements)
  drains: Drain[]          // per link restoration
  lastAtShore: number | null
}

export function computeMetrics(events: Delivered[], links: LinkChange[], now: number, mode: 'anchor' | 'baseline'): Metrics {
  const gen = events.filter(e => e.generatedAt <= now)
  const atShore = gen.filter(e => e.shoreAt !== null && e.shoreAt <= now)
  const atCoast = gen.filter(e => e.coastAt !== null && e.coastAt <= now)
  // never forwarded anywhere: the mock knows this up front; a live run knows it once the run is closed
  const dropped = (e: Delivered) => e.shoreAt === null && e.coastAt === null && e.deviceId !== 'edge-operator'

  // seq completeness at shore, per (device, bootId)
  const ranges = new Map<string, { min: number; max: number; n: number }>()
  const seen = new Set<string>()
  let duplicates = 0
  for (const e of atShore) {
    if (seen.has(e.eventId)) { duplicates++; continue }
    seen.add(e.eventId)
    if (!isCounted(e)) continue
    const k = stream(e), g = ranges.get(k)
    ranges.set(k, g ? { min: Math.min(g.min, e.seq!), max: Math.max(g.max, e.seq!), n: g.n + 1 } : { min: e.seq!, max: e.seq!, n: 1 })
  }
  let seqGaps = 0
  ranges.forEach(g => { seqGaps += g.max - g.min + 1 - g.n })
  const boots = new Map<string, Set<string>>()
  for (const k of ranges.keys()) {
    const [device, boot] = k.split(':')
    boots.set(device, (boots.get(device) ?? new Set()).add(boot))
  }

  const liveRecs = atShore.filter(e => !isHeld(e))
  const heldRecs = atShore.filter(isHeld)
  const delay = (e: Delivered) => e.shoreAt! - e.generatedAt

  // drains: after a link came back, how long until what waited behind it had crossed
  // Only an up that follows a down is a restoration. The first report of a link (mock run start, or the
  // moment the live monitor first reads a core) is the initial state, even when it lands after the first event.
  const drains: Drain[] = []
  const wasUp = new Map<LinkId, boolean>()
  for (const c of [...links].sort((a, b) => a.t - b.t)) {
    const before = wasUp.get(c.link)
    wasUp.set(c.link, c.up)
    if (!c.up || before !== false || c.t > now) continue
    const crossed = (e: Delivered) => (c.link === 'A' ? e.coastAt : e.shoreAt)
    const waited = (e: Delivered) => {
      const ready = c.link === 'A' ? e.generatedAt : e.coastAt
      const done = crossed(e)
      return ready !== null && ready < c.t && done !== null && done >= c.t
    }
    const items = events.filter(waited)
    const doneTimes = items.map(e => crossed(e)!).filter(t => t <= now)
    drains.push({ link: c.link, up: c.t, items: items.length,
      firstAfter: doneTimes.length ? Math.min(...doneTimes) - c.t : null,
      drained: items.length && doneTimes.length === items.length ? Math.max(...doneTimes) - c.t : null })
  }

  return {
    generated: gen.length,
    coastCustody: atCoast.length,
    shoreConfirmed: atShore.length,
    heldOnShip: mode === 'anchor' ? gen.filter(e => e.coastAt !== null && e.coastAt > now).length : 0,
    waitingAtCoast: atCoast.filter(e => e.shoreAt === null || e.shoreAt > now).length,
    lost: gen.filter(dropped).length,
    seqGaps, streams: ranges.size,
    reboots: [...boots.values()].filter(s => s.size > 1).length,
    duplicates,
    live: stats(liveRecs.map(delay)),
    hopA: stats(liveRecs.filter(e => e.coastAt !== null).map(e => e.coastAt! - e.generatedAt)),
    hopB: stats(liveRecs.filter(e => e.coastAt !== null).map(e => e.shoreAt! - e.coastAt!)),
    held: stats(heldRecs.map(delay)),
    heldCritical: stats(heldRecs.filter(e => e.priority === 'critical').map(delay)),
    drains,
    lastAtShore: atShore.length ? Math.max(...atShore.map(e => e.shoreAt!)) : null,
  }
}

export type CommandStep = 'issued' | 'stored at coast' | 'at ship' | 'applied' | 'expired'

/** where a shore command stands at `now`, from the steps observed so far */
export function commandStep(c: Command, now: number): CommandStep {
  if (c.appliedAt !== null && c.appliedAt <= now) return 'applied'
  if (now >= c.expiresAt) return 'expired'
  if (c.deliveredToShip !== null && c.deliveredToShip <= now) return 'at ship'
  if (c.cachedAtCoast !== null && c.cachedAtCoast <= now) return 'stored at coast'
  return 'issued'
}

export const fmtMs = (ms: number | null) =>
  ms === null ? '—' : ms < 1000 ? `${Math.round(ms)} ms` : ms < 60_000 ? `${(ms / 1000).toFixed(1)} s` : `${(ms / 60_000).toFixed(1)} min`

export const n = (v: number) => v.toLocaleString('en-US')
