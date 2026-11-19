// MEC (coastal) dashboard: the fleet-wide picture. The live map and fleet AI live here; clicking
// through goes to the single-vessel IN-CSE dashboard (same app, same theme — this view is /?mec).
import { useEffect, useRef, useState } from 'react'
import type { FleetConflict, FleetInfo } from '../types'
import { clock } from '../theme'
import { fmtMs } from '../lib/metrics'

const FLEET_STATE: Record<string, { label: string; tone: 'ok' | 'warn' | 'bad' | 'off'; hint: string }> = {
  ok: { label: 'Fleet AI online', tone: 'ok', hint: 'fleetStatus arriving from the coastal MEC' },
  stale: { label: 'Fleet AI not responding', tone: 'warn', hint: 'no new fleetStatus' },
  none: { label: 'Fleet AI not running', tone: 'off', hint: 'fleetAI is not on the coastal CSE yet' },
  forbidden: { label: 'No permission', tone: 'bad', hint: 'ACP: CAnchorDashboard may not read fleetAI' },
  error: { label: 'Read error', tone: 'bad', hint: 'the coastal CSE answered with an error' },
  starting: { label: 'Checking…', tone: 'off', hint: 'first read pending' },
}

/** scalar fields of a fleet record, minus bookkeeping — field names are the fleet AI's own */
const fleetFacts = (v: Record<string, unknown>, skip: string[] = []) =>
  Object.entries(v).filter(([k, x]) =>
    !['eventId', 'seenAt', 'at', 'redacted', 'ts', ...skip].includes(k) &&
    (typeof x === 'string' || typeof x === 'number' || typeof x === 'boolean'))

/** the real vessel: the sim still calls it ship-001 — shown as the team name until the sim renames it */
const SHIP_RE = /\b(ship-001|ctrl-sea)\b/i
const nm = (v: unknown) => String(v ?? '—').replace(/ship-001/gi, 'ctrl-sea')

const num = (v: unknown, d = 1) =>
  typeof v === 'number' ? (Number.isInteger(v) ? String(v) : v.toFixed(d)) : String(v ?? '—')

/** one conflict row, by the fleet AI's own type field (unknown types fall back to generic key/value) */
function ConflictCell({ c }: { c: FleetConflict }) {
  const lvl = typeof c.level === 'string' ? c.level : null
  const pill = lvl ? <span className={`pill ai-${lvl === 'danger' || lvl === 'critical' ? 'critical' : 'warn'}`}>{lvl}</span> : null
  if (c.type === 'fleet_conflict' && Array.isArray(c.pair)) {
    return <>{pill} <b className="cf-kind">collision risk</b>{' '}
      <span className="ai-feature">vessels <em>{nm(c.pair[0])} ↔ {nm(c.pair[1])}</em></span>{' '}
      {c.cpaNm != null && <span className="ai-feature">CPA <em>{num(c.cpaNm)} nm</em></span>}{' '}
      {c.tcpaMin != null && <span className="ai-feature">TCPA <em>{num(c.tcpaMin, 0)} min</em></span>}</>
  }
  if (c.type === 'hazard_warning') {
    return <>{pill} <b className="cf-kind">{String(c.hazardType ?? 'hazard')}</b>{' '}
      <span className="ai-feature">ship <em>{nm(c.ship)}</em></span>{' '}
      {c.hazardName != null && <span className="ai-feature">name <em>{String(c.hazardName)}</em></span>}{' '}
      {c.inside === true
        ? <span className="ai-feature">inside <em>now</em></span>
        : c.etaMin != null && <span className="ai-feature">enters in <em>{num(c.etaMin, 0)} min</em></span>}{' '}
      {c.escapeCourseDeg != null && <span className="ai-feature">escape course <em>{num(c.escapeCourseDeg, 0)}°</em></span>}{' '}
      {String(c.action) === 'rerouted' && <span className="pill ai-normal">rerouted by AI</span>}</>
  }
  if (c.type === 'route_deviation') {
    return <>{pill} <b className="cf-kind">off route</b>{' '}
      <span className="ai-feature">ship <em>{nm(c.ship)}</em></span>{' '}
      {c.offRouteKm != null && <span className="ai-feature">off by <em>{num(c.offRouteKm)} km</em></span>}{' '}
      {c.modelVersion != null && <span className="ai-feature">model <em>v{String(c.modelVersion)}</em></span>}</>
  }
  return <>{pill}{' '}
    {fleetFacts(c, ['level']).map(([k, v]) => (
      <span key={k} className="ai-feature">{k} <em>{nm(v)}</em></span>
    ))}</>
}

function FleetCard({ fleet, now, live }: { fleet: FleetInfo; now: number; live: boolean }) {
  const st = FLEET_STATE[fleet.state] ?? FLEET_STATE.error
  const age = fleet.status?.seenAt ? now - fleet.status.seenAt : null
  const conflicts = fleet.conflicts.filter(c => c.at <= now)
  const shown = conflicts.slice(0, 40)
  return (
    <section className="panel">
      <header className="panel-head"><h3>Fleet (coastal AI)</h3>
        <span>{live ? 'coastal CSE fleetAI, fleetStatus every 30 s, conflicts when seen' : 'demo data'}</span></header>
      <div className={`ai-state ${st.tone}`} title={fleet.error ?? st.hint}><b>{st.label}</b><small>{fleet.error ?? st.hint}</small></div>
      {fleet.status && (() => {
        const s = fleet.status as Record<string, unknown>
        const cp = s.closestPair as Record<string, unknown> | undefined
        const rm = s.routeModel as Record<string, unknown> | undefined
        return (
          <dl className="ai-facts">
            {s.ships != null && <div><dt>Vessels watched</dt><dd>{String(s.ships)}</dd></div>}
            {cp != null && <div><dt>Closest pair</dt>
              <dd>{nm(cp.a)} ↔ {nm(cp.b)} · {num(cp.cpaNm)} nm / {num(cp.tcpaMin, 0)} min</dd></div>}
            <div><dt>Route model</dt><dd>{rm != null ? `v${String(rm.version)} · ${String(rm.ships)} ships` : 'not trained yet'}</dd></div>
            {fleetFacts(s, ['ships']).slice(0, 3).map(([k, v]) => (
              <div key={k}><dt>{k}</dt><dd>{String(v)}</dd></div>
            ))}
            <div><dt>Last status</dt><dd>{age === null ? '—' : `${fmtMs(age)} ago`}</dd></div>
          </dl>
        )
      })()}
      {conflicts.length > 0 && (
        <table className="ai-table">
          <thead><tr><th>Time (UTC)</th><th>Conflict</th></tr></thead>
          <tbody>
            {shown.map(c => (
              <tr key={c.eventId ?? c.at}>
                <td>{clock(c.at)}<small>{fmtMs(now - c.at)} ago</small></td>
                <td><ConflictCell c={c} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {fleet.state === 'ok' && conflicts.length === 0 && <p className="empty">No vessel conflict in this run.</p>}
    </section>
  )
}

/** GET /api/fl — the FL coordinator tree on the fl-cse, relayed by the monitor (read-only) */
interface FlView {
  task?: { tid?: string; fst?: string; rnds?: number; minc?: number }
  participants?: { pid: string; prf?: string; nsm?: number; fst?: string; hb?: string }[]
  rounds?: { rnd: number; fst?: string; mvr?: string }[]
  models?: { ver?: string; rnd?: number; ntr?: number; pts?: string }[]
  updates?: number; calib?: number; error?: string
}

function FlCard({ live }: { live: boolean }) {
  const [v, setV] = useState<FlView | null>(null)
  useEffect(() => {
    if (!live) return
    let stop = false
    const tick = async () => {
      try {
        const r = await fetch('/api/fl', { cache: 'no-store' })
        if (!stop) setV(r.ok ? await r.json() : null)
      } catch { if (!stop) setV(null) }
    }
    tick()
    const t = window.setInterval(tick, 5000)
    return () => { stop = true; window.clearInterval(t) }
  }, [live])
  if (!live || !v?.task?.tid) return null // no fl-cse configured: no panel
  const phase = v.task.fst === 'done' ? 'training complete'
    : v.task.fst?.includes(':') ? `round ${v.task.fst.split(':')[0].slice(1)} · ${v.task.fst.split(':')[1]}`
    : v.task.fst ?? '—'
  const latest = v.models?.[v.models.length - 1]
  const contributors = latest?.pts ? (JSON.parse(latest.pts) as string[]) : []
  return (
    <section className="panel">
      <header className="panel-head"><h3>Federated learning</h3>
        <span>fl-cse · model trees travel as CINs, sensor data never leaves the vessel</span></header>
      <div className="fl-top">
        <span className={`pill ${v.task.fst === 'done' ? 'ai-normal' : 'ai-link'}`}>{phase}</span>
        <div className="fl-rounds">
          {Array.from({ length: v.task.rnds ?? 0 }, (_, i) => {
            const r = v.rounds?.find(x => x.rnd === i + 1)
            return <i key={i} className={`fl-dot ${r?.fst ?? 'pending'}`} title={`round ${i + 1}: ${r?.fst ?? 'pending'}`} />
          })}
        </div>
        <small>{n2(v.updates)} tree uploads · {n2(v.calib)} calibrations</small>
      </div>
      <dl className="ai-facts">
        {latest && <div><dt>Global model</dt><dd>{latest.ver} · {latest.ntr} trees · {contributors.length} vessels</dd></div>}
        <div><dt>Quorum</dt><dd>{v.task.minc ?? '—'} of {v.participants?.length ?? 0}</dd></div>
      </dl>
      {(v.participants?.length ?? 0) > 0 && (
        <table className="ai-table">
          <thead><tr><th>Participant</th><th>Local windows</th><th>State</th></tr></thead>
          <tbody>
            {v.participants!.map(pt => (
              <tr key={pt.pid}><td>{nm(pt.pid)}<small>{pt.prf}</small></td>
                <td>{pt.nsm ?? '—'}<small>stay local</small></td>
                <td><span className={`pill ${pt.fst === 'uploaded' ? 'ai-normal' : pt.fst === 'failed' ? 'ai-critical' : 'ai-link'}`}>{pt.fst ?? '—'}</span></td></tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}

const n2 = (x?: number) => (x ?? 0).toLocaleString()

export function MecDashboard({ fleet, inUrl, now, live }: { fleet?: FleetInfo | null; inUrl?: string | null; now: number; live: boolean }) {
  // ui.in_url rides on /api/run, which disappears while the shore CSE has no open run;
  // /api/health answers regardless, so navigation never depends on the run state.
  const [healthIn, setHealthIn] = useState<string | null>(null)
  useEffect(() => {
    if (!live) return
    fetch('/api/health', { cache: 'no-store' })
      .then(r => (r.ok ? r.json() : null))
      .then(h => setHealthIn(h?.inUrl ?? null))
      .catch(() => undefined)
  }, [live])
  const IN = inUrl || healthIn || '/'
  // The map is someone else's page, but it is relayed same-origin (/map/), so a click on anything
  // labelled ship-001 inside it (marker tooltip, popup, the Ships list) can open the IN dashboard.
  const frameRef = useRef<HTMLIFrameElement>(null)
  useEffect(() => {
    const f = frameRef.current
    if (!f) return
    const hook = () => {
      try {
        const doc = f.contentDocument as (Document & { __anchorHook?: boolean }) | null
        if (!doc || doc.__anchorHook) return
        doc.__anchorHook = true
        // Leaflet name labels (tooltips) swallow no clicks by default — make them clickable
        const st = doc.createElement('style')
        st.textContent = '.leaflet-tooltip, .leaflet-tooltip * { pointer-events: auto !important; cursor: pointer; }'
        ;(doc.head ?? doc.documentElement).appendChild(st)
        doc.addEventListener('click', e => {
          const el = e.target as Element | null
          const hit = el?.closest?.('.leaflet-marker-icon, .leaflet-tooltip, .leaflet-popup, li, tr') ?? el
          const label = hit?.getAttribute?.('title') ?? hit?.getAttribute?.('alt') ??
            hit?.getAttribute?.('aria-label') ?? hit?.textContent ?? ''
          if (label.length < 200 && SHIP_RE.test(label)) window.top!.location.href = IN
        }, true)
      } catch { /* not same-origin (direct map URL): leave the map alone */ }
    }
    f.addEventListener('load', hook)
    const t = window.setInterval(hook, 1500)
    return () => { f.removeEventListener('load', hook); window.clearInterval(t) }
  }, [IN, live])
  return (
    <div className="page mec-page">
      <header className="mec-head">
        <h1>ANCHOR <span>MEC dashboard</span></h1>
        <small>coastal fleet view — mep1 kr-busan · map, fleet AI, conflicts</small>
        <a className="go-in" href={IN}>ctrl-sea (real) · IN dashboard</a>
      </header>
      <section className="panel map-embed mec-map">
        <header className="panel-head"><h3>Fleet map</h3>
          <span>every vessel and buoy on the coastal MEC, live</span></header>
        {live ? <iframe ref={frameRef} src="/map/" title="fleet map" />
          : <div className="cam-empty" style={{ height: 320, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
              <b>Live runs only</b></div>}
      </section>
      <FlCard live={live} />
      {fleet ? <FleetCard fleet={fleet} now={now} live={live} /> : (
        <section className="panel"><header className="panel-head"><h3>Fleet (coastal AI)</h3></header>
          <p className="empty">fleet.enabled is off in the monitor config.</p></section>
      )}
    </div>
  )
}
