// Live 3D hull, ported from the ship dashboard (hardware/ship-gateway-pi5/dashboard/index.html):
// dependency-free canvas renderer — 23 stations, superellipse bilge, painter's algorithm — driven here
// by the attitude records as they arrive at the shore (guide §10.5: cinRoll/cinPitch, no 20 Hz stream).
// The hull is semi-transparent so the three sensor nodes inside stay visible; the water plane never
// tilts, only the hull does.
import { useEffect, useMemo, useRef, useState } from 'react'
import type { Delivered } from '../types'
import { fmtMs } from '../lib/metrics'

const TILT_WARN = 15, TILT_BAD = 25
const YAW = Math.PI / 2 // display heading: hull turned 90° to the left

type Pt = [number, number, number]
type Face = { pts: Pt[]; color: string; alpha: number; kind: 'hull' | 'deck' | 'cabin' }

const HULL: Face[] = (() => {
  const XS = -1.15, XB = 1.4, B = 0.5, NS = 22, NP = 8, N = 2.6
  const stations: Pt[][] = []
  for (let i = 0; i <= NS; i++) {
    const u = i / NS, x = XS + (XB - XS) * u
    const b = u <= 0.4 ? B * (0.82 + 0.18 * Math.sin(u / 0.4 * Math.PI / 2)) : B * Math.sqrt(Math.max(0, 1 - ((u - 0.4) / 0.6) ** 2))
    const zd = 0.3 + 0.14 * u * u
    const zk = u < 0.72 ? -0.24 : -0.24 + 0.34 * ((u - 0.72) / 0.28) ** 2
    const sec: Pt[] = []
    for (let j = 0; j <= NP; j++) {
      const t = j / NP * Math.PI / 2
      sec.push([x, b * Math.pow(Math.sin(t), 2 / N), zd - (zd - zk) * Math.pow(Math.cos(t), 2 / N)])
    }
    stations.push(sec)
  }
  const faces: Face[] = []
  for (let i = 0; i < NS; i++) {
    for (let j = 0; j < NP; j++) {
      const a = stations[i][j], b = stations[i + 1][j], c = stations[i + 1][j + 1], d = stations[i][j + 1]
      const col = j < 3 ? '#8e2a20' : '#c63b2e' // antifouling below, red topsides
      faces.push({ pts: [a, b, c, d], color: col, alpha: 0.38, kind: 'hull' })
      faces.push({ pts: [a, b, c, d].map(([x, y, z]) => [x, -y, z] as Pt), color: col, alpha: 0.38, kind: 'hull' })
    }
  }
  const edge = stations.map(s => s[NP])
  faces.push({ pts: [...edge, ...edge.slice().reverse().map(([x, y, z]) => [x, -y, z] as Pt)], color: '#dfe3ea', alpha: 0.22, kind: 'deck' })
  const st = stations[0]
  faces.push({ pts: [...st, ...st.slice().reverse().map(([x, y, z]) => [x, -y, z] as Pt)], color: '#a8322a', alpha: 0.45, kind: 'hull' })
  const box = (x0: number, x1: number, y0: number, y1: number, z0: number, z1: number, color: string, alpha: number) => {
    const quads: Pt[][] = [
      [[x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]],
      [[x0, y0, z0], [x1, y0, z0], [x1, y0, z1], [x0, y0, z1]],
      [[x0, y1, z0], [x1, y1, z0], [x1, y1, z1], [x0, y1, z1]],
      [[x1, y0, z0], [x1, y1, z0], [x1, y1, z1], [x1, y0, z1]],
      [[x0, y0, z0], [x0, y1, z0], [x0, y1, z1], [x0, y0, z1]],
    ]
    quads.forEach(pts => faces.push({ pts, color, alpha, kind: 'cabin' }))
  }
  box(-0.62, 0.08, -0.27, 0.27, 0.32, 0.66, '#e9eef5', 0.32)
  box(-0.22, -0.17, -0.025, 0.025, 0.66, 1.3, '#9aa4b2', 0.9)
  return faces
})()

// where each ESP32 node sits inside the hull (boat frame)
const NODES3D = [
  { id: 'bridge', label: 'BRIDGE', p: [-0.27, 0, 0.45] as Pt },
  { id: 'engine', label: 'ENGINE', p: [-0.85, 0, 0.02] as Pt },
  { id: 'cargo', label: 'CARGO', p: [0.55, 0, 0.05] as Pt },
] as const

type NodeId = typeof NODES3D[number]['id']
type Level = 'ok' | 'warn' | 'bad'
const LEVEL_COLOR: Record<Level, string> = { ok: '#2fae76', warn: '#bd8c37', bad: '#d9604f' }
const CALLOUT_DIR: Record<NodeId, [number, number]> = { bridge: [0.17, -0.27], cargo: [-0.25, -0.2], engine: [0.2, 0.1] }

function xform([x, y, z]: Pt, r: number, p: number): Pt {
  const cr = Math.cos(r), sr = Math.sin(r), cp = Math.cos(p), sp = Math.sin(p)
  const y1 = y * cr - z * sr, z1 = y * sr + z * cr
  const x2 = x * cp + z1 * sp, z2 = -x * sp + z1 * cp
  const cy = Math.cos(YAW), sy = Math.sin(YAW)
  return [x2 * cy - y1 * sy, x2 * sy + y1 * cy, z2]
}

function rgba(hex: string, k: number, a: number) {
  const n = parseInt(hex.slice(1), 16)
  const c = (s: number) => Math.min(255, Math.round(((n >> s) & 255) * k))
  return `rgba(${c(16)},${c(8)},${c(0)},${a})`
}

function roundRect(g: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  g.beginPath()
  g.moveTo(x + r, y); g.arcTo(x + w, y, x + w, y + h, r); g.arcTo(x + w, y + h, x, y + h, r)
  g.arcTo(x, y + h, x, y, r); g.arcTo(x, y, x + w, y, r); g.closePath()
}

type Callout = { title: string; rows: [string, string, string?][] }
type Scene = {
  roll: number; pitch: number; live: boolean
  nodeLevel: Record<NodeId, Level>
  callouts: Partial<Record<NodeId, Callout>>
}

function drawBoat(cv: HTMLCanvasElement, boat: { roll: number; pitch: number }, scene: Scene, exaggerate: boolean) {
  const dpr = window.devicePixelRatio || 1
  const w = cv.clientWidth, h = cv.clientHeight
  if (!w || !h) return
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr) }
  const g = cv.getContext('2d')!
  g.setTransform(dpr, 0, 0, dpr, 0, 0)
  g.clearRect(0, 0, w, h)

  const C: Pt = [-2.4, -2.7, 1.55], len = (v: number[]) => Math.hypot(...v)
  const f = C.map(v => -v / len(C))
  const rl = Math.hypot(f[1], f[0]), r = [f[1] / rl, -f[0] / rl, 0]
  const u = [r[1] * f[2] - r[2] * f[1], r[2] * f[0] - r[0] * f[2], r[0] * f[1] - r[1] * f[0]]
  const scale = Math.min(w * 0.62, h) * 1.3, cx = w * 0.5, cy = h * 0.54
  const proj = (p: Pt): Pt => {
    const d = [p[0] - C[0], p[1] - C[1], p[2] - C[2]]
    const zc = d[0] * f[0] + d[1] * f[1] + d[2] * f[2]
    return [cx + scale * (d[0] * r[0] + d[1] * r[1] + d[2] * r[2]) / zc,
            cy - scale * (d[0] * u[0] + d[1] * u[1] + d[2] * u[2]) / zc, zc]
  }
  const poly = (pts: Pt[]) => { g.beginPath(); pts.forEach((q, i) => i ? g.lineTo(q[0], q[1]) : g.moveTo(q[0], q[1])); g.closePath() }

  // static water plane (world frame) with grid; the hull tilts against it
  const W = 2.8
  poly(([[-W, -W], [W, -W], [W, W], [-W, W]] as [number, number][]).map(([x, y]) => proj([x, y, 0])))
  g.fillStyle = 'rgba(40,110,180,.20)'; g.fill()
  g.strokeStyle = 'rgba(120,180,255,.14)'; g.lineWidth = 1
  for (let t = -W; t <= W + 1e-6; t += W / 7) {
    let a = proj([t, -W, 0]), b = proj([t, W, 0]); g.beginPath(); g.moveTo(a[0], a[1]); g.lineTo(b[0], b[1]); g.stroke()
    a = proj([-W, t, 0]); b = proj([W, t, 0]); g.beginPath(); g.moveTo(a[0], a[1]); g.lineTo(b[0], b[1]); g.stroke()
  }

  const k = exaggerate ? 3 : 1, D = Math.PI / 180
  const ro = boat.roll * k * D, pi = boat.pitch * k * D
  const L = [-0.3, -0.5, 0.81]
  type Item = { type: 'face'; sp: Pt[]; depth: number; face: Face; lit: number }
            | { type: 'node'; q: Pt; depth: number; nd: typeof NODES3D[number] }
  const items: Item[] = HULL.map(face => {
    const pts = face.pts.map(p => xform(p, ro, pi))
    const a = pts[0], b = pts[1], c = pts[2]
    const e1 = [b[0] - a[0], b[1] - a[1], b[2] - a[2]], e2 = [c[0] - a[0], c[1] - a[1], c[2] - a[2]]
    let n = [e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0]]
    const nl = len(n) || 1; n = n.map(v => v / nl)
    const m = pts.length, ctr = pts.reduce((s, q) => [s[0] + q[0] / m, s[1] + q[1] / m, s[2] + q[2] / m], [0, 0, 0])
    const toCam = [C[0] - ctr[0], C[1] - ctr[1], C[2] - ctr[2]]
    if (n[0] * toCam[0] + n[1] * toCam[1] + n[2] * toCam[2] < 0) n = n.map(v => -v)
    const lit = 0.5 + 0.6 * Math.max(0, n[0] * L[0] + n[1] * L[1] + n[2] * L[2])
    return { type: 'face', sp: pts.map(proj), depth: proj(ctr as Pt)[2], face, lit }
  })
  NODES3D.forEach(nd => {
    const q = proj(xform(nd.p, ro, pi))
    items.push({ type: 'node', q, depth: q[2], nd })
  })
  items.sort((a, b) => b.depth - a.depth)

  const nodeScreen: Partial<Record<NodeId, { x: number; y: number; rad: number }>> = {}
  for (const it of items) {
    if (it.type === 'face') {
      poly(it.sp)
      g.fillStyle = rgba(it.face.color, it.lit, it.face.alpha); g.fill()
      g.strokeStyle = it.face.kind === 'hull' ? 'rgba(255,190,180,.10)' : 'rgba(255,255,255,.18)'
      g.lineWidth = 0.6; g.stroke()
    } else {
      const col = LEVEL_COLOR[scene.nodeLevel[it.nd.id]], [x, y] = it.q, rad = Math.max(5, scale * 0.022 / (it.q[2] / 3.8))
      const glow = g.createRadialGradient(x, y, 0, x, y, rad * 3)
      glow.addColorStop(0, col + 'aa'); glow.addColorStop(1, col + '00')
      g.fillStyle = glow; g.beginPath(); g.arc(x, y, rad * 3, 0, Math.PI * 2); g.fill()
      g.fillStyle = col; g.beginPath(); g.arc(x, y, rad, 0, Math.PI * 2); g.fill()
      g.strokeStyle = 'rgba(255,255,255,.7)'; g.lineWidth = 1; g.stroke()
      nodeScreen[it.nd.id] = { x, y, rad }
    }
  }

  // callouts: leader line from each node to a floating value box, with a travelling pulse
  const t = performance.now() / 1000
  for (const id of ['bridge', 'cargo', 'engine'] as NodeId[]) {
    const n = nodeScreen[id], c = scene.callouts[id]
    if (!n || !c) continue
    const col = LEVEL_COLOR[scene.nodeLevel[id]]
    const [dx, dy] = CALLOUT_DIR[id]
    const padX = 11, rowH = 19, titleH = 18
    g.font = '600 10px Pretendard, system-ui, sans-serif'
    const titleW = g.measureText(c.title).width
    let labW = 0, valW = 0
    for (const [lab, val] of c.rows) {
      g.font = '11px Pretendard, system-ui, sans-serif'; labW = Math.max(labW, g.measureText(lab).width)
      g.font = '600 13px Pretendard, system-ui, sans-serif'; valW = Math.max(valW, g.measureText(val).width)
    }
    const bw = Math.max(titleW, labW + 14 + valW) + padX * 2
    const bh = titleH + c.rows.length * rowH + 10
    const ax = n.x + dx * w, ay = n.y + dy * h
    let bx = dx >= 0 ? ax : ax - bw, by = ay - bh / 2
    bx = Math.max(8, Math.min(w - bw - 8, bx)); by = Math.max(8, Math.min(h - bh - 8, by))
    const ex = dx >= 0 ? bx : bx + bw, ey = by + bh / 2
    const kx = ex - (dx >= 0 ? 22 : -22), ky = ey
    g.strokeStyle = col; g.globalAlpha = 0.85; g.lineWidth = 1.4
    g.beginPath(); g.moveTo(n.x + Math.sign(kx - n.x) * n.rad * 0.7, n.y + Math.sign(ky - n.y) * n.rad * 0.7)
    g.lineTo(kx, ky); g.lineTo(ex, ey); g.stroke()
    const ph = (t * 0.6 + id.length * 0.13) % 1
    const px = n.x + (kx - n.x) * ph, py = n.y + (ky - n.y) * ph
    g.fillStyle = col; g.beginPath(); g.arc(px, py, 2.2, 0, Math.PI * 2); g.fill()
    g.globalAlpha = 1
    roundRect(g, bx, by, bw, bh, 4)
    g.fillStyle = 'rgba(8,22,36,.88)'; g.fill()
    g.strokeStyle = col; g.lineWidth = 1.2; g.stroke()
    g.fillStyle = col; g.fillRect(bx + 1, by + 6, 3, bh - 12)
    g.fillStyle = '#8fa6b5'; g.font = '600 10px Pretendard, system-ui, sans-serif'; g.textBaseline = 'middle'
    g.fillText(c.title, bx + padX, by + 5 + titleH / 2 - 2)
    c.rows.forEach(([lab, val, vcol], i) => {
      const yy = by + titleH + 5 + rowH * i + rowH / 2 - 2
      g.fillStyle = '#a9b6cc'; g.font = '11px Pretendard, system-ui, sans-serif'; g.fillText(lab, bx + padX, yy)
      g.fillStyle = vcol || '#e6edf7'; g.font = '600 13px Pretendard, system-ui, sans-serif'
      g.textAlign = 'right'; g.fillText(val, bx + bw - padX, yy); g.textAlign = 'left'
    })
    g.textBaseline = 'alphabetic'
  }

  const bow = proj(xform([1.42, 0, 0.44], ro, pi))
  g.fillStyle = '#8696b0'; g.font = '11px Pretendard, system-ui, sans-serif'; g.fillText('BOW ▸', bow[0] - 18, bow[1] - 10)
}

const num = (v: unknown) => (typeof v === 'number' ? v : null)

// The ship dashboard's live attitude stream (20 Hz SSE), relayed by the Shore Monitor on the same
// origin (no CORS, no extra certificate). When unreachable the hull follows the shore records.
const LIVE_SSE = '/live.sse'

export function ShipHull3D({ events, now, height = 300 }: { events: Delivered[]; now: number; height?: number }) {
  const [exaggerate, setExaggerate] = useState(true)

  // latest record per (device, sensor) as the shore has seen it, plus per-node freshness
  const scene: Scene & { age: number | null } = useMemo(() => {
    const latest: Record<string, Delivered> = {}
    const seen: Record<string, number> = {}
    for (let i = events.length - 1; i >= 0; i--) {
      const e = events[i]
      if (e.shoreAt === null || e.shoreAt > now) continue
      const key = `${e.deviceId}/${e.sensor}`
      if (!(key in latest)) latest[key] = e
      if (!(e.deviceId in seen)) seen[e.deviceId] = e.shoreAt
      if (Object.keys(latest).length >= 8 && Object.keys(seen).length >= 3 && latest['bridge/attitude']) break
    }
    const level = (id: NodeId): Level => {
      const at = seen[id]
      if (at === undefined) return 'bad'
      const age = now - at
      return age <= 10_000 ? 'ok' : age <= 30_000 ? 'warn' : 'bad' // same thresholds as the ship dashboard
    }
    const att = latest['bridge/attitude'], vib = latest['engine/vibration'], gas = latest['engine/gas']
    const cli = latest['cargo/climate'], hat = latest['cargo/hatch']
    const roll = num(att?.payload.roll) ?? 0, pitch = num(att?.payload.pitch) ?? 0
    const age = att?.shoreAt != null ? now - att.shoreAt : null
    const deg = (v: unknown) => (num(v) === null ? '—' : `${num(v)!.toFixed(1)}°`)
    return {
      roll, pitch, age, live: age !== null && age <= 30_000,
      nodeLevel: { bridge: level('bridge'), engine: level('engine'), cargo: level('cargo') },
      callouts: {
        bridge: { title: 'BRIDGE · S3', rows: [['Roll', deg(att?.payload.roll)], ['Pitch', deg(att?.payload.pitch)]] },
        engine: { title: 'ENGINE · WROOM', rows: [
          ['Vib RMS', num(vib?.payload.vib_rms_g) === null ? '—' : `${num(vib!.payload.vib_rms_g)!.toFixed(3)} g`],
          ['MQ-2', num(gas?.payload.mq2_v) === null ? '—' : `${num(gas!.payload.mq2_v)!.toFixed(2)} V`]] },
        cargo: { title: 'CARGO · C6', rows: [
          ['Temp', num(cli?.payload.temp_c) === null ? '—' : `${num(cli!.payload.temp_c)!.toFixed(1)} °C`],
          ['Hatch', typeof hat?.payload.state === 'string' ? hat.payload.state :
            typeof cli?.payload.hatch === 'string' ? String(cli.payload.hatch) : '—']] },
      },
    }
  }, [events, now])

  const canvasRef = useRef<HTMLCanvasElement>(null)
  const sceneRef = useRef(scene); sceneRef.current = scene
  const exagRef = useRef(exaggerate); exagRef.current = exaggerate
  const boat = useRef({ roll: 0, pitch: 0 })
  const live20 = useRef<{ roll: number; pitch: number; at: number } | null>(null)
  const [streaming, setStreaming] = useState(false)

  // direct 20 Hz feed from the ship (works once Pi 5 sends CORS headers; otherwise stays silent)
  useEffect(() => {
    let es: EventSource | null = null
    try { es = new EventSource(LIVE_SSE) } catch { return }
    es.onmessage = ev => {
      try {
        const m = JSON.parse(ev.data)
        if (typeof m?.data?.r === 'number' && String(m.topic).endsWith('bridge/attitude'))
          live20.current = { roll: m.data.r, pitch: m.data.p ?? 0, at: performance.now() }
      } catch { /* not a reading */ }
    }
    return () => es?.close()
  }, [])

  useEffect(() => {
    let raf = 0, last = performance.now()
    const loop = (t: number) => {
      const dt = Math.min(0.1, (t - last) / 1000); last = t
      const a = 1 - Math.exp(-dt / 0.07) // same easing as on the ship: ~70 ms time constant
      const s = sceneRef.current, b = boat.current
      const lv = live20.current, fresh = lv !== null && t - lv.at < 1500
      setStreaming(cur => cur === fresh ? cur : fresh)
      const tr = fresh ? lv!.roll : s.roll, tp = fresh ? lv!.pitch : s.pitch
      b.roll += (tr - b.roll) * a
      b.pitch += (tp - b.pitch) * a
      if (canvasRef.current) drawBoat(canvasRef.current, b, s, exagRef.current)
      raf = requestAnimationFrame(loop)
    }
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    if (reduced) { // one static frame, redrawn when data changes
      const tick = () => { boat.current = { roll: sceneRef.current.roll, pitch: sceneRef.current.pitch }
        if (canvasRef.current) drawBoat(canvasRef.current, boat.current, sceneRef.current, exagRef.current) }
      tick(); const id = setInterval(tick, 1000)
      return () => clearInterval(id)
    }
    raf = requestAnimationFrame(loop)
    return () => cancelAnimationFrame(raf)
  }, [])

  const worst = Math.max(Math.abs(scene.roll), Math.abs(scene.pitch))
  const tiltCls = scene.live && worst >= TILT_BAD ? ' bad' : scene.live && worst >= TILT_WARN ? ' warn' : ''
  return (
    <section className="panel ship-hull">
      <header className="panel-head"><h3>Ship attitude</h3>
        <span>3D hull from the ship dashboard, driven by attitude records as received at the shore</span></header>
      <div className={`hull-stage${scene.live ? '' : ' stale'}${tiltCls}`} style={{ height }}>
        <canvas ref={canvasRef} role="img"
          aria-label={scene.age === null ? 'no attitude data yet' : `roll ${scene.roll}°, pitch ${scene.pitch}°`} />
        <div className="ship-readout">
          <div><dt>Roll</dt><dd className={worst >= TILT_BAD ? 'bad' : ''}>{scene.age === null ? '—' : `${scene.roll.toFixed(1)}°`}</dd></div>
          <div><dt>Pitch</dt><dd className={worst >= TILT_BAD ? 'bad' : ''}>{scene.age === null ? '—' : `${scene.pitch.toFixed(1)}°`}</dd></div>
          <div><dt>Seen</dt><dd>{scene.age === null ? 'no data' : `${fmtMs(scene.age)} ago`}</dd></div>
        </div>
        {worst >= TILT_BAD && scene.live && <span className="ship-flag">TILT ≥ {TILT_BAD}°</span>}
        <span className={`hull-src ${streaming ? 'live' : scene.live ? 'cse' : 'off'}`}>
          {streaming ? 'live 20 Hz (ship)' : scene.live ? 'shore records 1 Hz' : 'frozen'}</span>
        {!scene.live && !streaming && scene.age !== null && <span className="ship-stale-note">frozen, last report {fmtMs(scene.age)} ago</span>}
      </div>
      <label className="ship-toggle">
        <input type="checkbox" checked={exaggerate} onChange={e => setExaggerate(e.target.checked)} />
        exaggerate motion ×3 (display only)
      </label>
    </section>
  )
}
