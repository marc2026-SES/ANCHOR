import { useMemo } from 'react'
import { EChart } from './EChart'
import { commandStep, computeMetrics, fmtMs, isHeld, n, type Drain, type Metrics } from '../lib/metrics'
import { exportEvents, exportLinks } from '../lib/csv'
import type { Run } from '../types'
import { C, FONT, clock, TOOLTIP } from '../theme'

/** whiskers at p5 / p95 so a long tail shows without single outliers dominating */
function fiveNum(xs: number[]): number[] {
  const s = [...xs].sort((a, b) => a - b)
  const q = (p: number) => s[Math.min(s.length - 1, Math.floor(p * (s.length - 1)))]
  return s.length ? [q(0.05), q(0.25), q(0.5), q(0.75), q(0.95)] : []
}

const delays = (run: Run, pick: (held: boolean, critical: boolean) => boolean) => run.events
  .filter(e => e.shoreAt !== null && pick(isHeld(e), e.priority === 'critical'))
  .map(e => e.shoreAt! - e.generatedAt)

function drains(m: Metrics, link: 'A' | 'B') {
  const ds = m.drains.filter(d => d.link === link && d.items > 0)
  if (!ds.length) return '—'
  const one = (d: Drain) => `${clock(d.up).slice(0, 5)} → ${fmtMs(d.drained)} (${n(d.items)})`
  if (ds.length <= 3) return ds.map(one).join(', ')
  // a flapping link (live run) gives many restores: first two, then the count and the longest drain
  const done = ds.filter(d => d.drained !== null)
  const longest = done.length ? done.reduce((a, b) => (b.drained! > a.drained! ? b : a)) : null
  return `${ds.slice(0, 2).map(one).join(', ')}, +${ds.length - 2} more` + (longest ? `, longest ${one(longest)}` : '')
}

function incidentDelay(run: Run) {
  const e = run.events.find(x => x.kind === 'incident' && x.payload.status === 'opened')
  if (!e) return 'no incident'
  return e.shoreAt === null ? 'never reached shore' : fmtMs(e.shoreAt - e.generatedAt)
}

function commandResult(run: Run) {
  const c = run.commands[run.commands.length - 1]
  if (!c) return '—'
  const step = commandStep(c, run.end + 3_600_000)
  return step === 'applied' ? `applied ${fmtMs(c.appliedAt! - c.issuedAt)} after issue` : step
}

export function MeasurementTab({ anchor, baseline, note = 'demo run, full run, dummy data' }: { anchor: Run; baseline: Run; note?: string }) {
  const runs = [baseline, anchor]
  const m = useMemo(() => runs.map(r => computeMetrics(r.events, r.links, r.end + 600_000, r.mode)), [anchor, baseline])

  const rows: { label: string; how: string; v: (x: Metrics, r: Run) => string }[] = [
    { label: 'Records generated', how: 'events in the ship journal', v: x => n(x.generated) },
    { label: 'Reached the shore', how: 'copies in cse-in/anchor/events', v: x => `${n(x.shoreConfirmed)} (${((x.shoreConfirmed / x.generated) * 100).toFixed(1)}%)` },
    { label: 'Records lost', how: 'never forwarded, seq gaps per (device, bootId) at the end', v: x => `${n(x.lost)}, gaps ${n(x.seqGaps)}` },
    { label: 'Live delay p50 / p95', how: 'shoreAt − generatedAt, records that never waited', v: x => `${fmtMs(x.live.p50)} / ${fmtMs(x.live.p95)}` },
    { label: 'Held data delay p50 / max', how: 'the same, records that waited on the ship or at the coast', v: x => `${fmtMs(x.held.p50)} / ${fmtMs(x.held.max)}` },
    { label: 'Tilt incident at shore', how: 'incident generated → shore copy (critical first)', v: (_, r) => incidentDelay(r) },
    { label: 'Backlog drain (link A)', how: 'link up → last waiting record stored at the coast', v: x => drains(x, 'A') },
    { label: 'Backlog drain (link B)', how: 'link up → last waiting record stored at the shore', v: x => drains(x, 'B') },
    { label: 'Shore command', how: 'issued at shore → ESP32 result "applied" within TTL', v: (_, r) => commandResult(r) },
    { label: 'Reboots / duplicates', how: 'new bootId per node, same eventId twice', v: x => `${x.reboots} / ${x.duplicates}` },
  ]

  const option = useMemo(() => {
    const cats = ['Live\nbaseline', 'Live\nANCHOR', 'Held\nANCHOR', 'Held critical\nANCHOR']
    const boxes = [
      delays(baseline, h => !h), delays(anchor, h => !h), delays(anchor, h => h), delays(anchor, (h, c) => h && c),
    ].map(fiveNum)
    return {
      textStyle: { fontFamily: FONT },
      animation: false,
      grid: { left: 64, right: 16, top: 16, bottom: 40 },
      tooltip: { ...TOOLTIP, trigger: 'item', formatter: (p: { name: string; value: number[] }) => {
        const v = Array.isArray(p.value) ? p.value.slice(-5) : []
        return `${p.name.replace('\n', ' ')}<br/>p5 ${fmtMs(v[0] ?? null)}, median ${fmtMs(v[2] ?? null)}, p95 ${fmtMs(v[4] ?? null)}`
      } },
      xAxis: { type: 'category', data: cats, axisLabel: { fontSize: 11, color: C.muted, interval: 0 }, axisLine: { lineStyle: { color: C.line } } },
      yAxis: { type: 'log', min: 100, max: 1_000_000, axisLabel: { fontSize: 10, color: C.muted, formatter: (v: number) => fmtMs(v) },
        splitLine: { lineStyle: { color: 'rgba(126,180,214,0.12)' } } },
      series: [{ type: 'boxplot', data: boxes.map((b, i) => ({ value: b, itemStyle: {
        color: i === 0 ? 'rgba(143,166,181,0.15)' : i === 1 ? 'rgba(63,154,208,0.18)' : 'rgba(178,115,200,0.18)',
        borderColor: i === 0 ? C.muted : i === 1 ? C.blue : C.recovered } })) }],
    }
  }, [anchor, baseline])

  return (
    <div className="tab-body measurement">
      <div className="row two">
        <section className="panel">
          <header className="panel-head"><h3>ANCHOR vs baseline</h3><span>{note}</span></header>
          <table className="grid compare">
            <thead><tr><th>Metric</th><th>Baseline</th><th>ANCHOR</th><th>How it is computed</th></tr></thead>
            <tbody>
              {rows.map(r => (
                <tr key={r.label}>
                  <td><b>{r.label}</b></td>
                  <td className="num">{r.v(m[0], baseline)}</td>
                  <td className="num strong">{r.v(m[1], anchor)}</td>
                  <td className="how">{r.how}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="note">Baseline = live forwarding without custody: whatever is generated while A or B is down is never delivered.</p>
        </section>
        <section className="panel">
          <header className="panel-head"><h3>Delay to the shore</h3><span>generated → shore, p25–p75 box, p5–p95 whiskers, log scale</span></header>
          <EChart option={option} height={300} />
          <p className="note">Held records wait for the next contact of A and then B; critical records (incident, command results) go first in every batch.</p>
        </section>
      </div>
      <section className="panel">
        <header className="panel-head"><h3>Runs</h3><span>result files</span></header>
        <table className="grid">
          <thead><tr><th>Run</th><th>Mode</th><th>Events</th><th>Link changes</th><th>Export</th></tr></thead>
          <tbody>
            {runs.map(r => (
              <tr key={r.runId}>
                <td className="mono">{r.runId}</td>
                <td>{r.mode === 'anchor' ? 'ANCHOR' : 'Baseline'}</td>
                <td className="num">{n(r.events.length)}</td>
                <td className="num">{r.links.length}</td>
                <td>
                  <button className="link-btn" onClick={() => exportEvents(r.runId, r.mode, r.events)}>events.csv</button>
                  <button className="link-btn" onClick={() => exportLinks(r.runId, r.links)}>links.csv</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="note">Pool-test runs are added here once recorded. The completeness check can also use the core batch check (capture-batch / batch-status).</p>
      </section>
    </div>
  )
}
