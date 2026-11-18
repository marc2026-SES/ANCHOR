// Live ship attitude: an original stern-view silhouette that rolls and bobs with the BRIDGE
// MPU6050 records as the shore receives them. When link A is down the boat freezes (like the
// camera feed) — honest: the shore simply knows nothing newer.
import { useMemo, useState } from 'react'
import type { Delivered } from '../types'
import { fmtMs } from '../lib/metrics'

const TILT_DEG = 25 // tilt-incident rule threshold (tilt-duration-v1)

export function ShipMotion({ events, now, height = 230 }: { events: Delivered[]; now: number; height?: number }) {
  const [exaggerate, setExaggerate] = useState(true)

  const last = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      const e = events[i]
      if (e.deviceId === 'bridge' && e.sensor === 'attitude' && e.shoreAt !== null && e.shoreAt <= now) return e
    }
    return null
  }, [events, now])

  const roll = typeof last?.payload.roll === 'number' ? last.payload.roll : null
  const pitch = typeof last?.payload.pitch === 'number' ? last.payload.pitch : null
  const age = last?.shoreAt != null ? now - last.shoreAt : null
  const stale = age === null || age > 15_000
  const mult = exaggerate ? 3 : 1
  const shownRoll = (roll ?? 0) * mult
  const bob = Math.max(-14, Math.min(14, (pitch ?? 0) * mult * 1.6))
  const tilted = roll !== null && pitch !== null && Math.max(Math.abs(roll), Math.abs(pitch)) >= TILT_DEG

  return (
    <section className="panel ship-motion">
      <header className="panel-head">
        <h3>Ship attitude</h3>
        <span>BRIDGE MPU6050 as received at the shore · freezes while the ship is cut off</span>
      </header>
      <div className={`ship-stage${stale ? ' stale' : ''}`} style={{ height }}>
        <svg viewBox="0 0 340 190" preserveAspectRatio="xMidYMid meet" role="img"
          aria-label={roll === null ? 'no attitude data yet' : `roll ${roll}°, pitch ${pitch}°`}>
          {/* horizon reference */}
          <line x1="12" y1="112" x2="328" y2="112" className="horizon" />
          <ellipse cx="170" cy="151" rx="64" ry="6.5" className="boat-shadow" />
          {/* the boat (rolls about the waterline, bobs with pitch) */}
          <g className="boat" style={{ transform: `translateY(${bob}px) rotate(${shownRoll}deg)` }}>
            <g className={tilted ? 'hull tilted' : 'hull'}>
              <path d="M 96 112 L 110 146 Q 170 160 230 146 L 244 112 Z" className="hull-body" />
              <rect x="128" y="86" width="84" height="26" rx="5" className="cabin" />
              <rect x="142" y="94" width="14" height="9" rx="2" className="window" />
              <rect x="163" y="94" width="14" height="9" rx="2" className="window" />
              <rect x="184" y="94" width="14" height="9" rx="2" className="window" />
              <line x1="170" y1="86" x2="170" y2="46" className="mast" />
              <circle cx="170" cy="42" r="4" className="beacon" />
            </g>
          </g>
          {/* water: two drifting decorative wave bands drawn above the hull base */}
          <g className="waves">
            <path className="wave w1" d="M -340 118 Q -297 110 -255 118 T -170 118 T -85 118 T 0 118 T 85 118 T 170 118 T 255 118 T 340 118 T 425 118 T 510 118 T 595 118 T 680 118 L 680 190 L -340 190 Z" />
            <path className="wave w2" d="M -340 128 Q -297 121 -255 128 T -170 128 T -85 128 T 0 128 T 85 128 T 170 128 T 255 128 T 340 128 T 425 128 T 510 128 T 595 128 T 680 128 L 680 190 L -340 190 Z" />
          </g>
        </svg>
        <div className="ship-readout">
          <div><dt>Roll</dt><dd className={tilted ? 'bad' : ''}>{roll === null ? '—' : `${roll.toFixed(1)}°`}</dd></div>
          <div><dt>Pitch</dt><dd className={tilted ? 'bad' : ''}>{pitch === null ? '—' : `${pitch.toFixed(1)}°`}</dd></div>
          <div><dt>Seen</dt><dd>{age === null ? 'no data' : `${fmtMs(age)} ago`}</dd></div>
        </div>
        {tilted && <span className="ship-flag">TILT ≥ {TILT_DEG}°</span>}
        {stale && age !== null && <span className="ship-stale-note">frozen · last report {fmtMs(age)} ago</span>}
      </div>
      <label className="ship-toggle">
        <input type="checkbox" checked={exaggerate} onChange={e => setExaggerate(e.target.checked)} />
        exaggerate motion ×3 (display only)
      </label>
    </section>
  )
}
