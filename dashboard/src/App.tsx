import { useEffect, useMemo, useState } from 'react'
import { buildRun } from './mock/scenario'
import { computeMetrics, linkUpAt, type Metrics } from './lib/metrics'
import { AUTH_NEEDED, setApiToken, useLiveRun } from './lib/live'
import { StatusBar, Playback } from './components/StatusBar'
import { KpiCards } from './components/KpiCards'
import { Tabs, type TabId } from './components/Tabs'
import { AiAlerts, AiTab } from './components/AiTab'
import { demoAi } from './mock/ai'
import { ContinuityTimeline } from './components/ContinuityTimeline'
import { NetworkPanel } from './components/NetworkPanel'
import { ZoneChart, type Marker, type SeriesSpec } from './components/ZoneChart'
import { QualityPanel } from './components/QualityPanel'
import { EventLog } from './components/EventLog'
import { MeasurementTab } from './components/MeasurementTab'
import { SecurityTab } from './components/SecurityTab'
import type { Delivered, LinkId, LinkState, Run } from './types'
import { C, LINK, clock } from './theme'

const TAIL = 60_000 // keep playing a moment after the run so the last records can arrive
/** ?live = read the Shore Monitor AE (server/shore_monitor.py) through /api instead of the dummy run */
const LIVE = new URLSearchParams(window.location.search).has('live')
const LINKS: LinkId[] = ['A', 'B']

function emptyRun(now: number): Run {
  return { runId: 'connecting…', mode: 'anchor', start: now - 60_000, end: now, events: [], links: [], commands: [], feed: [] }
}

/** link pills: the cores' report while following live, otherwise the recorded changes */
function linkStates(run: Run, now: number, following: boolean): Record<LinkId, LinkState> {
  const out = {} as Record<LinkId, LinkState>
  for (const l of LINKS) {
    if (LIVE && following) {
      const v = run.live?.links[l]
      out[l] = v && v.known && v.up !== null ? (v.up ? 'up' : 'down') : 'unknown'
    } else if (LIVE && !run.links.some(c => c.link === l && c.t <= now)) {
      out[l] = 'unknown'
    } else {
      out[l] = linkUpAt(run.links, l, now) ? 'up' : 'down'
    }
  }
  return out
}

/** which pills come from a gap in the arrivals instead of a core report */
function linkInferred(run: Run, now: number, following: boolean): Record<LinkId, boolean> {
  const out = {} as Record<LinkId, boolean>
  for (const l of LINKS) {
    const last = [...run.links].reverse().find(c => c.link === l && c.t <= now)
    out[l] = LIVE && following ? run.live?.links[l]?.source === 'inferred' : last?.source === 'inferred'
  }
  return out
}

/** the shore cannot see what is still on the ship; while following, take it from the ship core's receipts */
function liveHeld(run: Run, m: Metrics, following: boolean): { ship: number | null; coast: number | null } {
  const b = run.live?.backlog
  const coastRead = run.live?.sources.coast === true
  return {
    ship: following ? (b?.heldOnShip ?? null) : m.heldOnShip,
    coast: coastRead || !following ? m.waitingAtCoast : (b?.waitingAtCoast ?? null),
  }
}

const NODES: { device: string; title: string; subtitle: string; series: SeriesSpec[] }[] = [
  { device: 'bridge', title: 'BRIDGE', subtitle: 'MPU6050 roll · pitch (°) · 1 Hz',
    series: [{ sensor: 'attitude', field: 'roll', label: 'Roll', every: 1_000 },
      { sensor: 'attitude', field: 'pitch', label: 'Pitch', every: 1_000, color: LINK.A.color }] },
  { device: 'engine', title: 'ENGINE', subtitle: 'ADXL345 vibration RMS (g) · 1 Hz · MQ-2 voltage (V, not ppm) · 0.5 Hz',
    series: [{ sensor: 'vibration', field: 'vib_rms_g', label: 'Vibration', every: 1_000 },
      { sensor: 'gas', field: 'mq2_v', label: 'MQ-2', every: 2_000, axis: 1, color: LINK.A.color }] },
  { device: 'cargo', title: 'CARGO', subtitle: 'DHT11 temperature (°C) · 5 s · MC-38 hatch changes',
    series: [{ sensor: 'climate', field: 'temp_c', label: 'Temperature', every: 5_000 },
      { sensor: 'hatch', field: 'state', label: 'Hatch', every: 30_000, axis: 1, events: true }] },
]

function markers(device: string, events: Delivered[], now: number): Marker[] {
  const seen = events.filter(e => e.shoreAt !== null && e.shoreAt <= now)
  if (device === 'bridge') {
    return seen.filter(e => e.kind === 'incident').map(e => ({ t: e.generatedAt, color: C.red,
      label: e.payload.status === 'opened' ? 'tilt' : '' }))
      .concat(seen.filter(e => e.kind === 'commandResult' && e.deviceId === 'bridge')
        .map(e => ({ t: e.generatedAt, color: C.blue, label: `${String(e.payload.appliedIntervalMs ?? 200)} ms` })))
  }
  if (device === 'engine') {
    const boots = new Map<string, number>()
    for (const e of seen.filter(e => e.deviceId === 'engine' && e.seq !== null)) {
      boots.set(e.bootId, Math.min(boots.get(e.bootId) ?? Infinity, e.generatedAt))
    }
    return [...boots.values()].sort((a, b) => a - b).slice(1).map(t => ({ t, color: C.muted, label: 'reboot' }))
  }
  return []
}

export default function App() {
  const [mode, setMode] = useState<'anchor' | 'baseline'>('anchor')
  const [tab, setTab] = useState<TabId>('overview')
  const runs = useMemo(() => ({ anchor: buildRun('anchor'), baseline: buildRun('baseline') }), [])
  const live = useLiveRun(LIVE)
  const [clockNow, setClockNow] = useState(Date.now())
  useEffect(() => {
    if (!LIVE) return
    const id = setInterval(() => setClockNow(Date.now()), 1_000)
    return () => clearInterval(id)
  }, [])
  const liveNow = clockNow + live.skew
  const run = LIVE ? (live.run ?? emptyRun(liveNow)) : runs[mode]
  const start = run.start
  const end = LIVE ? Math.max(liveNow, run.end) : run.end + TAIL
  const [scrub, setNow] = useState(end)
  const [following, setFollowing] = useState(LIVE)
  const now = LIVE && following ? end : scrub
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState(60)

  useEffect(() => {
    if (!playing) return
    const id = setInterval(() => {
      setNow(v => {
        const next = v + 250 * speed
        if (next >= end) { setPlaying(false); return end }
        return next
      })
    }, 250)
    return () => clearInterval(id)
  }, [playing, speed, end])

  const computed = useMemo(() => computeMetrics(run.events, run.links, now, run.mode), [run, now])
  const held = LIVE ? liveHeld(run, computed, following) : null
  // live: nothing is known to be dropped until the run ends, so "lost" = seq gaps at the shore so far
  const metrics: Metrics = held
    ? { ...computed, lost: computed.seqGaps, heldOnShip: held.ship ?? 0, waitingAtCoast: held.coast ?? 0,
        generated: computed.generated + (following ? held.ship ?? 0 : 0) }
    : computed
  const atShore = useMemo(() => run.events.filter(e => e.shoreAt !== null && e.shoreAt <= now), [run, now])
  const link = linkStates(run, now, following)
  const ai = useMemo(() => (LIVE ? run.ai : demoAi(run)), [run])
  const otherMode = run.mode === 'anchor' ? 'baseline' : 'anchor'

  return (
    <div className="page">
      {/* always visible, whatever the tab */}
      <StatusBar runId={run.runId} link={link} inferred={linkInferred(run, now, following)} now={now} lastAtShore={metrics.lastAtShore}
        heldOnShip={held ? held.ship : metrics.heldOnShip} waitingAtCoast={held ? held.coast : metrics.waitingAtCoast}
        mode={run.mode} onMode={LIVE ? undefined : setMode} />
      {LIVE && <LiveBanner error={live.error} run={live.run} />}
      <Playback start={start} end={end} now={now} playing={playing} speed={speed}
        onSeek={v => { setFollowing(false); setNow(v) }} onPlay={setPlaying} onSpeed={setSpeed}
        live={LIVE ? { following, onFollow: () => setFollowing(true),
          hint: `live · Shore Monitor AE · run ${run.runId} since ${clock(start).slice(0, 5)} UTC · ${run.events.length.toLocaleString('en-US')} records` } : undefined} />
      <KpiCards m={metrics} command={run.commands[run.commands.length - 1]} now={now} />
      <AiAlerts ai={ai} now={now} onOpen={() => setTab('ai')} />
      <Tabs active={tab} onChange={setTab} />

      {tab === 'overview' && (
        <div className="tab-body overview">
          <ContinuityTimeline events={run.events} links={run.links} start={start} end={end} now={now} height={540} mode={run.mode} />
          <div className="side">
            <NetworkPanel link={link} m={metrics} mode={run.mode} held={held ?? undefined} />
            <EventLog feed={run.feed} now={now} />
          </div>
        </div>
      )}

      {tab === 'sensors' && (
        <div className="tab-body zones">
          {NODES.map(nd => (
            <ZoneChart key={nd.device} title={nd.title} subtitle={nd.subtitle} series={nd.series}
              records={atShore.filter(e => e.deviceId === nd.device)} links={run.links}
              start={start} end={end} markers={markers(nd.device, run.events, now)} height={230} />
          ))}
          <QualityPanel events={run.events} now={now} />
        </div>
      )}

      {tab === 'measurement' && (LIVE
        ? <MeasurementTab anchor={run.mode === 'anchor' ? run : runs.anchor} baseline={run.mode === 'baseline' ? run : runs.baseline}
            note={`${run.mode === 'anchor' ? 'ANCHOR' : 'Baseline'}: live run ${run.runId} · ${otherMode === 'anchor' ? 'ANCHOR' : 'Baseline'}: dummy data`} />
        : <MeasurementTab anchor={runs.anchor} baseline={runs.baseline} />)}
      {tab === 'ai' && <AiTab ai={ai} now={now} live={LIVE} />}
      {tab === 'security' && <SecurityTab />}

      {tab === 'sensors' && (
        <footer className="legend">
          <span><i className="sw live" /> reached the shore live</span>
          <span><i className="sw late" /> reached the shore after waiting (ship or coast)</span>
          <span><i className="sw a-down" /> link A down</span>
          <span><i className="sw b-down" /> link B down</span>
          <span>x-axis = generation time on the node</span>
        </footer>
      )}
    </div>
  )
}

/** what the monitor itself reports: unreachable, or a source it cannot read */
function TokenForm() {
  const [value, setValue] = useState('')
  const [sent, setSent] = useState(false)
  return (
    <form className="live-banner token-form" onSubmit={e => { e.preventDefault(); setApiToken(value); setSent(true) }}>
      <label htmlFor="api-token">{sent ? 'That token was not accepted. Try again:' : 'Shore Monitor API token:'}</label>
      <input id="api-token" type="password" autoComplete="off" value={value} onChange={e => setValue(e.target.value)} />
      <button type="submit" disabled={!value.trim()}>Connect</button>
      <small>Kept in this tab only. On the Pi: cat server/secrets/api-token</small>
    </form>
  )
}

function LiveBanner({ error, run }: { error: string | null; run: Run | null }) {
  if (error === AUTH_NEEDED) return <TokenForm />
  const s = run?.live?.sources
  const problems = [
    error && `Shore monitor not reachable (${error}). Start server/shore_monitor.py.`,
    s && s.shore === false && 'The monitor cannot read the shore journal (address or ACP).',
    s && s.subscription === false && 'Subscription refused: the monitor scans every 2 s instead.',
    s && s.coast === false && 'Coast journal not readable: hop A / B split and coast custody unknown.',
    run && !run.live && run.end && 'This run is closed.',
  ].filter(Boolean)
  if (!problems.length) return null
  return <div className="live-banner">{problems.map((p, i) => <span key={i}>{p}</span>)}</div>
}
