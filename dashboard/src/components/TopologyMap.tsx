// Live network diagram, bridge-HUD style: the three ESP32 nodes feed the ship Pi, link A carries
// records to the coastal MEC, link B to the shore Pi, and this dashboard reads the shore journal.
// Link lines pulse while a link is up and break (red, ×) while it is down; counts are the same
// figures as the KPI row, placed where the data physically sits.
import type { LinkId, LinkState } from '../types'
import type { Metrics } from '../lib/metrics'
import { n } from '../lib/metrics'

function Node({ x, y, w = 150, h = 74, title, sub, value, note }:
  { x: number; y: number; w?: number; h?: number; title: string; sub: string; value?: string; note?: string }) {
  return (
    <g className="tp-node">
      <rect x={x - w / 2} y={y} width={w} height={h} rx="10" />
      <path d={`M ${x - w / 2} ${y + 12} v -12 h 12`} className="tp-bracket" />
      <path d={`M ${x + w / 2} ${y + h - 12} v 12 h -12`} className="tp-bracket dim" />
      <text x={x} y={y + 19} textAnchor="middle" className="tp-title">{title}</text>
      <text x={x} y={y + 33} textAnchor="middle" className="tp-sub">{sub}</text>
      {value !== undefined && <text x={x} y={y + 55} textAnchor="middle" className="tp-value">{value}</text>}
      {note && <text x={x} y={y + 68} textAnchor="middle" className="tp-sub">{note}</text>}
    </g>
  )
}

function Sensor({ x, y, title, sub }: { x: number; y: number; title: string; sub: string }) {
  return (
    <g className="tp-sensor">
      <rect x={x - 62} y={y - 15} width={124} height={34} rx="8" />
      <circle cx={x - 48} cy={y + 2} r="3.5" className="tp-dot" />
      <text x={x - 38} y={y + 0} className="tp-title small">{title}</text>
      <text x={x - 38} y={y + 12} className="tp-sub">{sub}</text>
    </g>
  )
}

function LinkLine({ x1, x2, y, id, state, inferred }:
  { x1: number; x2: number; y: number; id: LinkId; state: LinkState; inferred?: boolean }) {
  const mid = (x1 + x2) / 2
  const cls = state === 'up' ? 'up' : state === 'down' ? 'down' : 'unknown'
  return (
    <g className={`tp-link ${cls}`}>
      <line x1={x1} y1={y} x2={x2} y2={y} className="tp-rail" />
      {state === 'up' && <line x1={x1} y1={y} x2={x2} y2={y} className="tp-flow" />}
      {state === 'down' && <text x={mid} y={y + 5} textAnchor="middle" className="tp-cross">×</text>}
      <text x={mid} y={y - 14} textAnchor="middle" className="tp-linklabel">
        LINK {id} · {state === 'unknown' ? 'no report' : state}{inferred && state !== 'unknown' ? ' · inferred' : ''}
      </text>
    </g>
  )
}

export function TopologyMap({ link, inferred, m, held }: {
  link: Record<LinkId, LinkState>; inferred?: Record<LinkId, boolean>; m: Metrics
  held?: { ship: number | null; coast: number | null }
}) {
  const heldShip = held ? held.ship : m.heldOnShip
  const waiting = held ? held.coast : m.waitingAtCoast
  const Y = 86 // node tops
  const LY = Y + 37 // link line height
  return (
    <section className="panel tp">
      <header className="panel-head"><h3>Network</h3>
        <span>three sensor nodes → ship → coast → shore · counts sit where the data sits · pulses = records flowing</span></header>
      <svg viewBox="0 0 1180 238" className="tp-svg" role="img" aria-label="network diagram">
        {/* ghost blueprint of the vessel behind the diagram */}
        <g className="tp-ghost" aria-hidden="true">
          <path d="M 150 196 Q 400 214 700 206 Q 950 198 1046 150 L 1010 124 L 190 124 Q 160 160 150 196 Z" />
          <path d="M 300 124 L 300 84 L 560 84 L 560 124" />
          <path d="M 560 124 L 560 56 L 680 56 L 680 124" />
          <path d="M 580 56 H 660 M 580 70 H 660" className="tp-ghost-fine" />
          <path d="M 320 100 H 540" className="tp-ghost-fine" />
          <path d="M 730 124 L 730 30 M 718 44 H 742" />
          <path d="M 860 124 L 900 92 L 940 124" className="tp-ghost-fine" />
          <circle cx="260" cy="156" r="7" /><circle cx="360" cy="162" r="7" /><circle cx="460" cy="166" r="7" />
          <circle cx="560" cy="168" r="7" /><circle cx="660" cy="168" r="7" /><circle cx="760" cy="166" r="7" />
          <path d="M 60 185 H 1120" className="tp-ghost-water" />
        </g>
        {/* sensors → ship */}
        <Sensor x={92} y={LY - 58} title="BRIDGE · S3" sub="attitude · GPS" />
        <Sensor x={92} y={LY} title="ENGINE · WROOM" sub="vibration · MQ-2" />
        <Sensor x={92} y={LY + 58} title="CARGO · C6" sub="climate · hatch" />
        {[LY - 58, LY, LY + 58].map(sy => (
          <path key={sy} d={`M 154 ${sy + 2} C 210 ${sy + 2} 220 ${LY} 262 ${LY}`} className="tp-feed" />
        ))}
        <text x={205} y={LY + 64} textAnchor="middle" className="tp-sub">MQTT · boat LAN</text>

        <g className="tp-sonar" aria-hidden="true">
          <circle cx={337} cy={LY} r="16" className="s1" />
          <circle cx={337} cy={LY} r="16" className="s2" />
        </g>
        <Node x={337} y={Y} title="SHIP · Pi 5" sub="cse-ship · core"
          value={heldShip === null ? '—' : n(heldShip)} note="held on ship" />
        <LinkLine x1={412} x2={548} y={LY} id="A" state={link.A} inferred={inferred?.A} />
        <Node x={623} y={Y} title="COAST · MEC" sub="cse-edge · core · fleet-ai"
          value={waiting === null ? '—' : n(waiting)} note="waiting for B" />
        <LinkLine x1={698} x2={834} y={LY} id="B" state={link.B} inferred={inferred?.B} />
        <Node x={909} y={Y} title="SHORE · Pi 4" sub="cse-in · core · anchorAI copy"
          value={n(m.shoreConfirmed)} note="final receipts" />
        <path d={`M 984 ${LY} H 1038`} className="tp-feed" />
        <Node x={1108} y={Y + 8} w={136} h={58} title="DASHBOARD" sub="Shore Monitor AE" />
      </svg>
    </section>
  )
}
