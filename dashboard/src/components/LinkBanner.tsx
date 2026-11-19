// Phase banner + 10-minute link strip, ported from the hardware shore dashboard (shore.html):
// the cut must be seen the moment it happens, and the recovery must visibly snap back in.
//   ONLINE      records arriving, shown with the current delivery delay
//   OUTAGE      nothing arriving — red, with the outage duration counting up
//   CATCHING_UP held records pouring in after a cut — the recovery moment itself
// The strip paints the last 10 minutes of shore arrivals: green = flowing, red = the running outage,
// purple-outlined = an outage whose records were later delivered (ANCHOR), grey "lost" = baseline.
import { useEffect, useMemo, useRef } from 'react'
import type { Delivered } from '../types'
import type { Metrics } from '../lib/metrics'
import { isHeld, fmtMs, n } from '../lib/metrics'

const WIN = 600_000 // strip window, ms
const GAP = 12_000  // arrivals stopped for longer than this = outage

type Band = { a: number; b: number; live: boolean; recovered: number }
type Phase = 'WAITING' | 'ONLINE' | 'OUTAGE' | 'CATCHING_UP'

function analyse(events: Delivered[], now: number, backlog: number) {
  const t0 = now - WIN
  const arrivals: number[] = []
  let lateRecent = 0, lastArrival = -1
  for (const e of events) {
    if (e.shoreAt === null || e.shoreAt > now) continue
    if (e.shoreAt > lastArrival) lastArrival = e.shoreAt
    if (e.shoreAt >= t0) arrivals.push(e.shoreAt)
    if (e.shoreAt >= now - 8_000 && isHeld(e)) lateRecent++
  }
  arrivals.sort((a, b) => a - b)
  // gaps in the arrival stream
  const bands: Band[] = []
  let prev = t0
  for (const at of arrivals) {
    if (at - prev > GAP) bands.push({ a: prev, b: at, live: false, recovered: 0 })
    prev = Math.max(prev, at)
  }
  if (lastArrival >= 0 && now - lastArrival > GAP) bands.push({ a: Math.max(t0, lastArrival), b: now, live: true, recovered: 0 })
  // a completed gap is "recovered" when records generated inside it did reach the shore afterwards
  for (const b of bands) {
    if (b.live) continue
    for (const e of events) {
      if (e.generatedAt >= b.a && e.generatedAt <= b.b && e.shoreAt !== null && e.shoreAt <= now && e.shoreAt >= b.b - 2_000) b.recovered++
    }
  }
  const age = lastArrival < 0 ? null : now - lastArrival
  const phase: Phase = lastArrival < 0 ? 'WAITING'
    : age! > GAP ? 'OUTAGE'
    : lateRecent >= 3 ? 'CATCHING_UP'
    : 'ONLINE'
  return { bands, age, phase, lateRecent, backlog }
}

export function LinkBanner({ events, now, m, mode }: { events: Delivered[]; now: number; m: Metrics; mode: 'anchor' | 'baseline' }) {
  const sec = Math.floor(now / 1000) // recompute at most once a second
  const a = useMemo(() => analyse(events, now, Math.max(0, m.heldOnShip) + Math.max(0, m.waitingAtCoast)),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [events, sec, m.heldOnShip, m.waitingAtCoast])

  const canvasRef = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const cv = canvasRef.current
    if (!cv) return
    const dpr = window.devicePixelRatio || 1, w = cv.clientWidth, h = cv.clientHeight
    if (!w || !h) return
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr) }
    const g = cv.getContext('2d')!
    g.setTransform(dpr, 0, 0, dpr, 0, 0)
    g.clearRect(0, 0, w, h)
    const x = (t: number) => (t - (now - WIN)) / WIN * w
    // base: green while data was flowing (grey until the first record)
    g.fillStyle = a.age === null ? 'rgba(126,180,214,0.14)' : 'rgba(63,154,208,0.5)'
    g.fillRect(0, 0, w, h)
    g.font = '600 10px Pretendard, system-ui, sans-serif'
    g.textBaseline = 'middle'
    // pixel-space cleanup: merge same-kind bands separated by slivers of green, enforce a visible width
    type Px = { xa: number; xb: number; live: boolean; rec: boolean; ms: number }
    const px: Px[] = []
    for (const b of a.bands) {
      const xa = Math.max(0, x(b.a)), xb = Math.min(w, x(b.b))
      if (xb <= 0 || xb - xa < 0.5) continue
      const rec = !b.live && b.recovered > 0
      const prev = px[px.length - 1]
      if (prev && !prev.live && !b.live && prev.rec === rec && xa - prev.xb < 6) {
        prev.xb = xb; prev.ms += b.b - b.a
      } else px.push({ xa, xb: Math.max(xb, xa + 2.5), live: b.live, rec, ms: b.b - b.a })
    }
    for (const b of px) {
      const { xa, xb } = b
      if (false) continue
      const rec = b.rec
      g.clearRect(xa, 0, xb - xa, h)
      g.fillStyle = b.live ? '#c0564a' : rec ? '#8a639c' : 'rgba(217,96,79,0.32)'
      g.fillRect(xa, 0, xb - xa, h)
      if (!rec) { // cuts are hatched (a hole in the data); recovered is solid (the hole filled back in)
        g.save(); g.beginPath(); g.rect(xa, 0, xb - xa, h); g.clip()
        g.strokeStyle = b.live ? 'rgba(255,255,255,0.18)' : 'rgba(255,255,255,0.1)'; g.lineWidth = 1
        for (let sx = xa - h; sx < xb; sx += 6) { g.beginPath(); g.moveTo(sx, h); g.lineTo(sx + h, 0); g.stroke() }
        g.restore()
      }
      const label = b.live ? `cut ${fmtMs(b.ms)}`
        : rec ? `${fmtMs(b.ms)} cut · recovered`
        : `${fmtMs(b.ms)} cut · ${mode === 'anchor' ? 'not back yet' : 'lost'}`
      if (xb - xa > g.measureText(label).width + 14) {
        g.fillStyle = b.live ? 'rgba(8,18,28,0.75)' : 'rgba(8,18,28,0.55)'
        g.fillRect(xa + 3, h / 2 - 7, g.measureText(label).width + 8, 14)
        g.fillStyle = b.live ? '#eef5fa' : '#b9c7d2'
        g.fillText(label, xa + 7, h / 2 + 0.5)
      }
    }
    g.textBaseline = 'alphabetic'
  }, [a, now, mode])

  const num = a.phase === 'OUTAGE' ? `${fmtMs(a.age ?? 0)} and counting`
    : a.phase === 'CATCHING_UP' ? `backlog ${n(a.backlog)} to go`
    : a.phase === 'ONLINE' ? `delay p50 ${fmtMs(m.live.p50)}` : ''
  const text = a.phase === 'WAITING' ? ['Waiting for ship data', 'no record has reached the shore yet']
    : a.phase === 'OUTAGE' ? ['Nothing arriving at the shore', mode === 'anchor' ? 'the ship keeps collecting — records arrive when a path returns' : 'baseline: records generated now are being lost']
    : a.phase === 'CATCHING_UP' ? ['Held records coming in', `${n(a.lateRecent)} recovered in the last 8 s — the cut is being repaid`]
    : ['Receiving live', 'records are flowing ship → coast → shore']
  return (
    <section className={`panel link-banner ${a.phase.toLowerCase()}`} role="status">
      <header className="panel-head"><h3>{text[0]}</h3><span>{text[1]}</span>
        <b className="lb-num">{num}</b></header>
      <div className="lb-legend">
        <i className="arriving" />arriving
        <i className="cut" />cut (now)
        <i className="notback" />{mode === 'anchor' ? 'cut, not back yet' : 'cut, lost'}
        <i className="rec" />recovered
      </div>
      <div className="lb-strip">
        <canvas ref={canvasRef} aria-label="shore arrivals, last 10 minutes" />
        <span className="lb-t0">-10 min</span><span className="lb-t1">now</span>
      </div>
    </section>
  )
}
