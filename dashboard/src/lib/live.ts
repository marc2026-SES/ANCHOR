// Live data from the Shore Monitor AE (server/shore_monitor.py), through the /api proxy of Vite.
// The monitor reads the shore journal as CAnchorDashboard; this file turns its /api/run answer
// into the same Run shape the mock produces, so every chart and metric runs unchanged.

import { useEffect, useRef, useState } from 'react'
import type { AiInfo, Command, Delivered, FeedItem, FleetInfo, LinkChange, LinkId, LiveInfo, Run } from '../types'

interface ApiCommand {
  commandId: string; target: string; intervalMs: number; durationMs: number
  issuedAtMs: number; expiresAtMs: number; coastAt: number | null; shoreAt: number | null
}
interface ApiRun {
  runId: string; mode: 'anchor' | 'baseline'; start: number; end: number | null; now: number; rev: number; after: number
  events: Delivered[]; commands: ApiCommand[]; links: LinkChange[]; live: LiveInfo | null; ai?: AiInfo | null; fleet?: FleetInfo | null; inUrl?: string | null
}

const POLL_MS = 2_000

/** API token typed into the page when the monitor serves the dashboard on the shore Pi.
 *  Memory only: never in the URL or browser storage (same rule as the core's control page). */
let apiToken = ''
export function setApiToken(token: string) { apiToken = token.trim() }
export const AUTH_NEEDED = 'auth'

/** a command is applied once every target device reported status applied (src/anchor/commands.py) */
function toCommand(c: ApiCommand, events: Delivered[]): Command {
  const targets = c.target === 'reportingGroup' ? ['bridge', 'engine'] : [c.target]
  const applied = targets.map(d => events.find(e => e.kind === 'commandResult' && e.deviceId === d &&
    e.payload.commandId === c.commandId && e.payload.status === 'applied'))
  const done = applied.every(Boolean)
  return {
    commandId: c.commandId, target: c.target, intervalMs: c.intervalMs, durationMs: c.durationMs,
    issuedAt: c.issuedAtMs, expiresAt: c.expiresAtMs, cachedAtCoast: c.coastAt,
    deliveredToShip: null, // the shore cannot see the ship journal; the device result proves delivery
    appliedAt: done ? Math.max(...applied.map(e => Number(e!.payload.appliedAtMs))) : null,
  }
}

const deg = (v: unknown) => (typeof v === 'number' ? `${v.toFixed(1)}°` : '?')
const short = (id: string) => (id.length > 14 ? id.slice(0, 12) + '…' : id)

/** the event log, built from what reached the shore */
export function buildFeed(events: Delivered[], links: LinkChange[], commands: Command[]): FeedItem[] {
  const feed: FeedItem[] = []
  const linkName: Record<LinkId, string> = { A: 'Link A (ship ↔ coast)', B: 'Link B (coast ↔ shore)' }
  for (const l of links) {
    feed.push({ t: l.t, kind: 'link', text: `${linkName[l.link]} ${l.up ? 'up' : 'down'}` +
      (l.source === 'inferred' ? ', inferred from a gap in the data' : '') })
  }
  const firstBoot = new Map<string, Map<string, number>>()
  for (const e of events) {
    const seen = e.shoreAt ?? undefined
    if (e.kind === 'incident') {
      feed.push({ t: e.generatedAt, seen, kind: 'incident', text: e.payload.status === 'opened'
        ? `Tilt incident opened, roll ${deg(e.payload.roll)} pitch ${deg(e.payload.pitch)}`
        : 'Tilt incident recovered' })
    } else if (e.kind === 'incidentAck') {
      feed.push({ t: e.generatedAt, seen, kind: 'incident',
        text: `Incident acknowledged at ${String(e.payload.site ?? '?')} by ${String(e.payload.operator ?? '?')}` })
    } else if (e.kind === 'commandResult') {
      feed.push({ t: Number(e.payload.appliedAtMs) || e.generatedAt, seen, kind: 'command',
        text: `${e.deviceId} ${String(e.payload.status)} ${short(String(e.payload.commandId))}` +
          (e.payload.status === 'applied' ? `, ${String(e.payload.appliedIntervalMs)} ms` : '') })
    }
    if (e.seq !== null && (e.kind === 'measurement' || e.kind === 'commandResult')) {
      const boots = firstBoot.get(e.deviceId) ?? new Map<string, number>()
      boots.set(e.bootId, Math.min(boots.get(e.bootId) ?? Infinity, e.generatedAt))
      firstBoot.set(e.deviceId, boots)
    }
  }
  for (const [device, boots] of firstBoot) {
    const starts = [...boots.entries()].sort((a, b) => a[1] - b[1]).slice(1)
    for (const [boot, t] of starts) feed.push({ t, kind: 'delivery', text: `${device} rebooted, new bootId ${boot}, seq restarts at 0` })
  }
  for (const c of commands) {
    feed.push({ t: c.issuedAt, kind: 'command',
      text: `Command ${short(c.commandId)} issued, ${c.target} every ${c.intervalMs} ms for ${c.durationMs / 1000} s` })
    if (c.cachedAtCoast !== null) feed.push({ t: c.cachedAtCoast, kind: 'command', text: `Command ${short(c.commandId)} stored at coast` })
  }
  return feed
}

export interface LiveState {
  run: Run | null
  /** server clock minus browser clock, so "now" follows the monitor and the CSEs, not the laptop */
  skew: number
  error: string | null
}

/** polls /api/run every 2 s; only events changed since the last answer are transferred */
export function useLiveRun(enabled: boolean): LiveState {
  const [state, setState] = useState<LiveState>({ run: null, skew: 0, error: null })
  const cache = useRef({ runId: '', rev: 0, events: new Map<string, Delivered>() })

  useEffect(() => {
    if (!enabled) return
    let stop = false
    let timer: ReturnType<typeof setTimeout>
    const tick = async () => {
      try {
        const c = cache.current
        const res = await fetch(`/api/run?after=${c.rev}`, {
          cache: 'no-store', headers: apiToken ? { Authorization: `Bearer ${apiToken}` } : undefined,
        })
        if (res.status === 401) throw new Error(AUTH_NEEDED)
        if (!res.ok) throw new Error(`monitor answered ${res.status}`)
        const api: ApiRun = await res.json()
        if (api.runId !== c.runId) { c.events.clear(); c.runId = api.runId; if (api.after !== 0) { c.rev = 0; return } }
        for (const e of api.events) c.events.set(e.eventId, e)
        c.rev = api.rev
        const events = [...c.events.values()].sort((a, b) => a.generatedAt - b.generatedAt)
        const links: LinkChange[] = api.links.map(l => ({ t: l.t, link: l.link, up: l.up, source: l.source }))
        const commands = api.commands.map(x => toCommand(x, events))
        const run: Run = {
          runId: api.runId, mode: api.mode, start: api.start, end: api.end ?? api.now,
          events, links, commands, feed: buildFeed(events, links, commands), live: api.live, ai: api.ai ?? null, fleet: api.fleet ?? null, inUrl: api.inUrl ?? null,
        }
        if (!stop) setState({ run, skew: api.now - Date.now(), error: null })
      } catch (err) {
        if (!stop) setState(s => ({ ...s, error: err instanceof Error ? err.message : String(err) }))
      } finally {
        if (!stop) timer = setTimeout(tick, POLL_MS)
      }
    }
    tick()
    return () => { stop = true; clearTimeout(timer) }
  }, [enabled])

  return state
}
