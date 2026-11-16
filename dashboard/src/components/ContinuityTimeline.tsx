import { useMemo } from 'react'
import { EChart } from './EChart'
import { bands } from './bands'
import { isHeld } from '../lib/metrics'
import type { Delivered, LinkChange } from '../types'
import { C, FONT, LINK, clock } from '../theme'

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
    const waiting = (pick: (e: Delivered, at: number) => boolean) => x.map(xi => {
      const at = xi + BUCKET / 2
      return [xi, at > now ? null : events.filter(e => pick(e, at)).length] as [number, number | null]
    })
    const onShip = waiting((e, at) => e.generatedAt <= at && e.coastAt !== null && e.coastAt > at)
    const atCoast = waiting((e, at) => e.coastAt !== null && e.coastAt <= at && (e.shoreAt === null || e.shoreAt > at))
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
        { name: 'Waiting at coast', type: 'line', yAxisIndex: 1, showSymbol: false, step: 'end',
          lineStyle: { color: LINK.B.color, width: 1.6 }, itemStyle: { color: LINK.B.color }, data: atCoast })
    }
    return {
      textStyle: { fontFamily: FONT },
      animation: false,
      grid: { left: 44, right: 48, top: 40, bottom: 26 },
      tooltip: { trigger: 'axis' },
      legend: { top: 4, left: 40, itemWidth: 14, itemHeight: 8, textStyle: { fontSize: 11, color: C.muted } },
      xAxis: { type: 'time', min: start, max: end, axisLabel: { fontSize: 10, color: C.muted, formatter: (v: number) => clock(v).slice(0, 5) },
        axisLine: { lineStyle: { color: C.line } } },
      yAxis: [
        { type: 'value', axisLabel: { fontSize: 10, color: C.muted }, splitLine: { lineStyle: { color: '#f0f0f2' } } },
        { type: 'value', axisLabel: { fontSize: 10, color: C.muted }, splitLine: { show: false } },
      ],
      series,
    }
  }, [events, links, start, end, now, mode])

  return (
    <section className="panel">
      <header className="panel-head"><h3>Data continuity</h3>
        <span>bars: records reaching the shore per 10 s (left) · lines: records still waiting (right) · amber band = A down · red band = B down</span></header>
      <EChart option={option} height={height} />
    </section>
  )
}
