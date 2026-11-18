// Where every generated record stands right now, as one ring: delivered live, still on the way
// (held on the ship or waiting at the coast), delivered after waiting, lost. Palette validated
// (CVD-safe adjacent pairs; blue and purple sit opposite, never touching).
import { useMemo } from 'react'
import type { Delivered } from '../types'
import { EChart } from './EChart'
import { isHeld } from '../lib/metrics'
import { C, FONT } from '../theme'

const SLICES = [
  { key: 'live', name: 'Delivered live', color: '#3f9ad0' },
  { key: 'transit', name: 'Still on the way', color: '#bd8c37' },
  { key: 'late', name: 'Delivered after waiting', color: '#b273c8' },
  { key: 'lost', name: 'Lost', color: '#d9604f' },
] as const

export function DeliveryDonut({ events, now, heldOnShip, waitingAtCoast, lost, height = 240 }:
  { events: Delivered[]; now: number; heldOnShip: number; waitingAtCoast: number; lost: number; height?: number }) {
  const option = useMemo(() => {
    let live = 0, late = 0
    for (const e of events) {
      if (e.shoreAt === null || e.shoreAt > now) continue
      if (isHeld(e)) late++
      else live++
    }
    const counts = { live, transit: Math.max(0, heldOnShip) + Math.max(0, waitingAtCoast), late, lost: Math.max(0, lost) }
    const total = counts.live + counts.transit + counts.late + counts.lost
    const arrived = counts.live + counts.late
    return {
      textStyle: { fontFamily: FONT },
      tooltip: {
        trigger: 'item',
        valueFormatter: (v: number) => `${v.toLocaleString('en-US')} records`,
      },
      legend: {
        bottom: 0, left: 'center', icon: 'circle', itemWidth: 9, itemHeight: 9,
        textStyle: { color: C.muted, fontSize: 11 },
      },
      series: [{
        type: 'pie',
        radius: ['58%', '82%'],
        center: ['50%', '44%'],
        avoidLabelOverlap: true,
        itemStyle: { borderColor: C.panel, borderWidth: 2, borderRadius: 3 },
        label: { show: false },
        emphasis: { scaleSize: 4, label: { show: false } },
        data: SLICES.map(s => ({
          name: s.name, value: counts[s.key],
          itemStyle: {
            color: s.color,
            // texture on the "waited" slice: identity never rests on hue alone (blue/purple can
            // end up adjacent when the other slices are empty)
            ...(s.key === 'late' ? { decal: { symbol: 'rect', color: 'rgba(255,255,255,0.35)',
              rotation: Math.PI / 4, symbolSize: 1, dashArrayX: [1, 0], dashArrayY: [2, 5] } } : {}),
          },
        })).filter(d => d.value > 0),
      }],
      graphic: [{
        type: 'text', left: 'center', top: '36%',
        style: {
          text: total ? `${Math.round((arrived / total) * 100)}%` : '—',
          font: `600 26px ${FONT}`, fill: C.ink, textAlign: 'center',
        },
      }, {
        type: 'text', left: 'center', top: '52%',
        style: { text: 'at the shore', font: `11px ${FONT}`, fill: C.muted, textAlign: 'center' },
      }],
    }
  }, [events, now, heldOnShip, waitingAtCoast, lost])

  return (
    <section className="panel">
      <header className="panel-head"><h3>Delivery mix</h3><span>every record generated on the ship, right now</span></header>
      <EChart option={option} height={height} />
    </section>
  )
}
