import { useMemo } from 'react'
import { EChart } from './EChart'
import { bands } from './bands'
import { isHeld } from '../lib/metrics'
import type { Delivered, LinkChange } from '../types'
import { C, FONT, LINK, clock, TOOLTIP } from '../theme'

const BUCKET = 10_000

/** Records reaching the shore per 10 s (live vs held), and what is still waiting on the ship and at the coast. */
export function ContinuityTimeline(props: {
  events: Delivered[]; links: LinkChange[]; start: number; end: number; now: number; height?: number
  mode: 'anchor' | 'baseline'
}) {
  const { events, links, start, end, now, height = 300, mode } = props
  const option = useMemo(() => {
    const nb = Math.ceil((end - start) / BUCKET)
    const live = new Array(nb).fill(0), late = new Array(nb).fill(0)
    for (const e of events) {
      if (e.shoreAt === null || e.shoreAt > now) continue
      const b = Math.floor((e.shoreAt - start) / BUCKET)
      if (b < 0 || b >= nb) continue
      if (isHeld(e)) late[b]++
      else live[b]++
    }
    const x = Array.from({ length: nb }, (_, i) => start + i * BUCKET + BUCKET / 2)
    // sampled at each bucket's end: how many records sat on the ship / at the MEC at that moment.
    // One pass with difference arrays — the old filter-per-bucket was O(events × buckets) and
    // froze the tab once a run grew to hours (2,000 buckets × 45k events).
    const idx = (t: number) => Math.min(nb, Math.max(0, Math.floor((t - start) / BUCKET)))
    const shipD = new Array(nb + 1).fill(0), coastD = new Array(nb + 1).fill(0)
    const span = (d: number[], from: number, to: number | null) => {
      const a = idx(from), b = to === null ? nb : idx(to)
      if (b > a) { d[a]++; d[b]-- }
    }
    for (const e of events) {
      if (e.coastAt !== null) {
        span(shipD, e.generatedAt, e.coastAt)
        span(coastD, e.coastAt, e.shoreAt)
      }
    }
    const sample = (d: number[]) => {
      let cur = 0
      return x.map((xi, i) => {
        cur += d[i]
        return [xi, xi + BUCKET / 2 > now ? null : cur] as [number, number | null]
      })
    }
    const onShip = sample(shipD)
    const atCoast = sample(coastD)
    const lab = (text: string, color: string) => ({ formatter: text, fontSize: 10, color, position: 'end' })
    const series: object[] = [
      { name: 'Reached shore live', type: 'bar', stack: 'r', barWidth: '90%', itemStyle: { color: C.blue },
        data: x.map((xi, i) => [xi, live[i]]),
        markArea: { silent: true, data: bands(links, end) },
        markLine: { silent: true, symbol: 'none', data: [{ xAxis: now, lineStyle: { color: C.ink, type: 'solid', width: 1 }, label: lab('now', C.ink) }] } },
      { name: 'Reached shore after waiting', type: 'bar', stack: 'r', itemStyle: { color: C.recovered }, data: x.map((xi, i) => [xi, late[i]]) },
    ]
    if (mode === 'anchor') {
      series.push(
        { name: 'Held on ship', type: 'line', yAxisIndex: 1, showSymbol: false, step: 'end',
          lineStyle: { color: LINK.A.color, width: 1.6 }, itemStyle: { color: LINK.A.color }, data: onShip },
        { name: 'Waiting at MEC', type: 'line', yAxisIndex: 1, showSymbol: false, step: 'end',
          lineStyle: { color: LINK.B.color, width: 1.6 }, itemStyle: { color: LINK.B.color }, data: atCoast })
    }
    return {
      textStyle: { fontFamily: FONT },
      animation: false,
      grid: { left: 44, right: 48, top: 40, bottom: 26 },
      tooltip: { ...TOOLTIP, trigger: 'axis' },
      legend: { top: 4, left: 40, itemWidth: 14, itemHeight: 8, textStyle: { fontSize: 11, color: C.muted } },
      xAxis: { type: 'time', min: start, max: end, axisLabel: { fontSize: 10, color: C.muted, formatter: (v: number) => clock(v).slice(0, 5) },
        axisLine: { lineStyle: { color: C.line } } },
      yAxis: [
        { type: 'value', axisLabel: { fontSize: 10, color: C.muted }, splitLine: { lineStyle: { color: 'rgba(126,180,214,0.12)' } } },
        { type: 'value', axisLabel: { fontSize: 10, color: C.muted }, splitLine: { show: false } },
      ],
      series,
    }
  }, [events, links, start, end, now, mode])

  return (
    <section className="panel">
      <header className="panel-head"><h3>Data continuity</h3>
        <span>bars are records reaching the shore per 10 s (left), lines are records still waiting (right), amber band = A down, red band = B down</span></header>
      <EChart option={option} height={height} />
    </section>
  )
}
