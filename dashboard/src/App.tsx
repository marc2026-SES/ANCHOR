import { useEffect, useMemo, useState } from 'react'
import { buildRun } from './mock/scenario'
import { computeMetrics } from './lib/metrics'
import { StatusBar, Playback } from './components/StatusBar'
import { KpiCards } from './components/KpiCards'
import { Tabs, type TabId } from './components/Tabs'
import { ContinuityTimeline } from './components/ContinuityTimeline'
import { NetworkPanel } from './components/NetworkPanel'
import { ZoneChart, type Marker, type SeriesSpec } from './components/ZoneChart'
import { QualityPanel } from './components/QualityPanel'
import { EventLog } from './components/EventLog'
import { MeasurementTab } from './components/MeasurementTab'
import { SecurityTab } from './components/SecurityTab'
import type { Delivered } from './types'
import { C, LINK } from './theme'

const TAIL = 60_000 // keep playing a moment after the run so the last records can arrive

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
        .map(e => ({ t: e.generatedAt, color: C.blue, label: '200 ms' })))
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
  const run = runs[mode]
  const start = run.start
  const end = run.end + TAIL
  const [now, setNow] = useState(end)
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

  const metrics = useMemo(() => computeMetrics(run.events, run.links, now, run.mode), [run, now])
  const atShore = useMemo(() => run.events.filter(e => e.shoreAt !== null && e.shoreAt <= now), [run, now])

  return (
    <div className="page">
      {/* always visible, whatever the tab */}
      <StatusBar runId={run.runId} links={run.links} now={now} lastAtShore={metrics.lastAtShore}
        heldOnShip={metrics.heldOnShip} waitingAtCoast={metrics.waitingAtCoast} mode={mode} onMode={setMode} />
      <Playback start={start} end={end} now={now} playing={playing} speed={speed}
        onSeek={setNow} onPlay={setPlaying} onSpeed={setSpeed} />
      <KpiCards m={metrics} command={run.commands[0]} now={now} />
      <Tabs active={tab} onChange={setTab} />

      {tab === 'overview' && (
        <div className="tab-body overview">
          <ContinuityTimeline events={run.events} links={run.links} start={start} end={end} now={now} height={540} mode={run.mode} />
          <div className="side">
            <NetworkPanel links={run.links} now={now} m={metrics} mode={run.mode} />
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

      {tab === 'measurement' && <MeasurementTab anchor={runs.anchor} baseline={runs.baseline} />}
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
