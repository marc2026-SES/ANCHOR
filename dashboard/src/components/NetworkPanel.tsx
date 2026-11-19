import type { LinkId, LinkState } from '../types'
import type { Metrics } from '../lib/metrics'
import { n } from '../lib/metrics'
import { C } from '../theme'

const W = 380, H = 150
const NODES = [
  { id: 'ship', x: 52, title: 'Ship', sub: 'Pi 5, ship CSE' },
  { id: 'coast', x: 190, title: 'MEC', sub: 'edge CSE, apps' },
  { id: 'shore', x: 328, title: 'Shore', sub: 'IN-CSE' },
] as const
const BW = 96, BH = 74, Y = 14

/** Where every record is right now: kept on the ship, in custody at the coast, or confirmed at the shore. */
export function NetworkPanel({ link, m, mode, held }: {
  link: Record<LinkId, LinkState>; m: Metrics; mode: 'anchor' | 'baseline'
  /** live: counts the shore cannot see (null = no report from the ship / coast core) */
  held?: { ship: number | null; coast: number | null }
}) {
  const ship = held ? held.ship : m.heldOnShip
  const coast = held ? held.coast : m.waitingAtCoast
  const counts: Record<string, [string, string]> = mode === 'anchor'
    ? { ship: [held ? (ship === null ? '—' : n(m.generated)) : n(m.generated), ship === null ? 'no ship report' : `${n(ship)} not yet at MEC`],
        coast: [n(m.coastCustody), coast === null ? 'no MEC report' : `${n(coast)} waiting for B`],
        shore: [n(m.shoreConfirmed), 'final receipts'] }
    : { ship: [n(m.generated), `${n(m.lost)} dropped`], coast: ['—', 'no custody'], shore: [n(m.shoreConfirmed), 'received'] }
  const line = (x1: number, x2: number, label: LinkId) => {
    const state = link[label]
    const ok = state === 'up'
    const color = ok ? C.up : state === 'down' ? C.down : C.muted
    return (
      <g key={label}>
        <line x1={x1} x2={x2} y1={Y + BH / 2} y2={Y + BH / 2} stroke={color} strokeWidth="2.5" strokeDasharray={ok ? undefined : '5 4'} />
        <text x={(x1 + x2) / 2} y={Y + BH / 2 - 8} textAnchor="middle" fontSize="11" fontWeight="700" fill={color}>{label}</text>
        <text x={(x1 + x2) / 2} y={Y + BH / 2 + 16} textAnchor="middle" fontSize="10" fill={color}>{state === 'unknown' ? 'no report' : state}</text>
      </g>
    )
  }
  return (
    <section className="panel">
      <header className="panel-head"><h3>Where the data is</h3><span>A and B never need to be up together</span></header>
      <svg viewBox={`0 0 ${W} ${H}`} className="net" role="img" aria-label="Ship, MEC and shore with links A and B">
        {line(NODES[0].x + BW / 2, NODES[1].x - BW / 2, 'A')}
        {line(NODES[1].x + BW / 2, NODES[2].x - BW / 2, 'B')}
        {NODES.map(nd => (
          <g key={nd.id}>
            <rect x={nd.x - BW / 2} y={Y} width={BW} height={BH} rx="8" fill="rgba(95,195,239,0.07)" stroke="rgba(126,180,214,0.35)" strokeWidth="1.2" />
            <text x={nd.x} y={Y + 17} textAnchor="middle" fontSize="12" fontWeight="700" fill={C.ink}>{nd.title}</text>
            <text x={nd.x} y={Y + 31} textAnchor="middle" fontSize="9.5" fill={C.muted}>{nd.sub}</text>
            <text x={nd.x} y={Y + 53} textAnchor="middle" fontSize="16" fontWeight="700" fill={C.blue}>{counts[nd.id][0]}</text>
            <text x={nd.x} y={Y + 67} textAnchor="middle" fontSize="9.5" fill={C.muted}>{counts[nd.id][1]}</text>
          </g>
        ))}
        <text x={W / 2} y={H - 24} textAnchor="middle" fontSize="10" fill={C.muted}>
          {mode === 'anchor' ? 'MEC custody and shore confirmation are separate receipts.' : 'Baseline forwards live only: no custody, no replay.'}
        </text>
        <text x={W / 2} y={H - 10} textAnchor="middle" fontSize="10" fill={C.muted}>
          records generated on the ship, stored at the MEC, confirmed at the shore
        </text>
      </svg>
    </section>
  )
}
