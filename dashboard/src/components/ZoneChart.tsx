import { useMemo } from 'react'
import { EChart } from './EChart'
import { bands } from './bands'
import { isHeld } from '../lib/metrics'
import type { Delivered, LinkChange, Sensor } from '../types'
import { C, FONT, clock, TOOLTIP } from '../theme'

export interface SeriesSpec {
  sensor: Sensor
  field: string
  label: string
  /** nominal period, for drawing gaps */
  every: number
  axis?: 0 | 1
  color?: string
  /** draw as event markers (y = 1 open / 0 closed) instead of a line */
  events?: boolean
}

export interface Marker { t: number; label: string; color: string }

/** Line data with gaps: a null wherever two points are further apart than 3 periods. */
function withGaps(rs: Delivered[], field: string, every: number): (number | null)[][] {
  const out: (number | null)[][] = []
  rs.forEach((r, i) => {
    const v = r.payload[field]
    if (typeof v !== 'number') return
    if (i > 0 && r.generatedAt - rs[i - 1].generatedAt > every * 3) out.push([rs[i - 1].generatedAt + every, null])
    out.push([r.generatedAt, v])
  })
  return out
}

export function ZoneChart(props: {
  title: string; subtitle: string; records: Delivered[]; series: SeriesSpec[]
  links: LinkChange[]; start: number; end: number; markers?: Marker[]; height?: number
}) {
  const { title, subtitle, records, series, links, start, end, markers = [], height = 190 } = props
  const option = useMemo(() => {
    const out: object[] = []
    series.forEach((s, si) => {
      const rs = records.filter(r => r.sensor === s.sensor).sort((a, b) => a.generatedAt - b.generatedAt)
      if (s.events) {
        const changes = rs.filter(r => r.payload.event === 'change')
        out.push({ name: s.label, type: 'scatter', yAxisIndex: s.axis ?? 0, symbol: 'diamond', symbolSize: 10,
          itemStyle: { color: C.red }, z: 5,
          data: changes.map(r => ({ value: [r.generatedAt, r.payload.state === 'open' ? 1 : 0], name: String(r.payload.state) })) })
        return
      }
      const color = s.color ?? C.blue
      out.push({ name: s.label, type: 'line', yAxisIndex: s.axis ?? 0, showSymbol: false, sampling: 'lttb',
        lineStyle: { width: 1.4, color }, itemStyle: { color },
        data: withGaps(rs.filter(r => !isHeld(r)), s.field, s.every),
        markArea: si === 0 ? { silent: true, data: bands(links, end) } : undefined,
        markLine: si === 0 && markers.length ? {
          silent: true, symbol: 'none',
          data: markers.map(m => ({ xAxis: m.t, lineStyle: { color: m.color, type: 'dashed', width: 1 },
            label: { show: !!m.label, formatter: m.label, color: m.color, fontSize: 10, position: 'insideEndTop' } })),
        } : undefined,
      })
      out.push({ name: `${s.label} (after waiting)`, type: 'line', yAxisIndex: s.axis ?? 0, showSymbol: false,
        lineStyle: { width: 1.4, color: C.recovered, type: si > 0 ? 'dashed' : 'solid' }, itemStyle: { color: C.recovered },
        data: withGaps(rs.filter(isHeld), s.field, s.every) })
    })
    const twoAxes = series.some(s => s.axis === 1)
    const eventAxis = series.some(s => s.axis === 1 && s.events)
    return {
      textStyle: { fontFamily: FONT },
      animation: false,
      grid: { left: 44, right: twoAxes && !eventAxis ? 44 : 14, top: 34, bottom: 24 },
      tooltip: { ...TOOLTIP, trigger: 'axis', valueFormatter: (v: number) => (v == null ? '—' : String(v)) },
      legend: { top: 4, right: 8, itemWidth: 14, itemHeight: 8, textStyle: { fontSize: 11, color: C.muted },
        data: series.map(s => s.label) },
      xAxis: { type: 'time', min: start, max: end, axisLabel: { fontSize: 10, color: C.muted, formatter: (v: number) => clock(v).slice(0, 5) },
        axisLine: { lineStyle: { color: C.line } }, splitLine: { show: false } },
      yAxis: [0, 1].slice(0, twoAxes ? 2 : 1).map(i => (i === 1 && eventAxis
        ? { type: 'value', min: -0.2, max: 1.2, show: false }
        : { type: 'value', scale: true, axisLabel: { fontSize: 10, color: C.muted },
            splitLine: { show: i === 0, lineStyle: { color: 'rgba(126,180,214,0.12)' } } })),
      series: out,
    }
  }, [records, series, links, start, end, markers])

  return (
    <section className="panel">
      <header className="panel-head"><h3>{title}</h3><span>{subtitle}</span></header>
      <EChart option={option} height={height} />
    </section>
  )
}
