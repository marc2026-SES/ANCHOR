import type { Delivered } from '../types'
import { isCounted, n } from '../lib/metrics'

interface Stream { device: string; bootId: string; min: number; max: number; count: number; last: number }

/** Per node: boot identity, sequence completeness at the shore and the sensor quality flags. */
export function QualityPanel({ events, now }: { events: Delivered[]; now: number }) {
  const atShore = events.filter(e => e.shoreAt !== null && e.shoreAt <= now)
  const streams = new Map<string, Stream>()
  for (const e of atShore.filter(isCounted)) {
    const k = `${e.deviceId}:${e.bootId}`
    const s = streams.get(k)
    if (!s) streams.set(k, { device: e.deviceId, bootId: e.bootId, min: e.seq!, max: e.seq!, count: 1, last: e.generatedAt })
    else Object.assign(s, { min: Math.min(s.min, e.seq!), max: Math.max(s.max, e.seq!), count: s.count + 1, last: Math.max(s.last, e.generatedAt) })
  }
  const latest = (device: string, sensor: string) =>
    atShore.filter(e => e.deviceId === device && e.sensor === sensor).sort((a, b) => b.generatedAt - a.generatedAt)[0]
  const pos = latest('bridge', 'position'), att = latest('bridge', 'attitude')
  const gas = latest('engine', 'gas')
  const climate = atShore.filter(e => e.sensor === 'climate')
  const dhtFail = climate.filter(e => e.payload.ok === false).length
  const hatch = latest('cargo', 'hatch')
  const quality: Record<string, string> = {
    bridge: `${pos ? (pos.quality?.validFix ? `GPS fix, ${pos.payload.sats} sats` : 'GPS no fix') : 'GPS —'}, report every ${att?.payload.reportIntervalMs ?? '—'} ms`,
    engine: gas ? (gas.quality?.warmed ? 'MQ-2 warmed (voltage, not ppm)' : 'MQ-2 warming up (first 2 min after boot)') : 'MQ-2 —',
    cargo: `DHT11 ${climate.length ? `${n(climate.length - dhtFail)}/${n(climate.length)} ok` : '—'}, hatch ${hatch?.payload.state ?? '—'}`,
  }
  const rows = [...streams.values()].sort((a, b) => a.device.localeCompare(b.device) || a.last - b.last)
  return (
    <section className="panel">
      <header className="panel-head"><h3>Sensor quality</h3><span>per node, as received at the shore</span></header>
      <table className="grid">
        <thead><tr><th>Node, boot</th><th>seq at shore</th><th>Gaps</th><th>Quality</th></tr></thead>
        <tbody>
          {rows.length === 0 && <tr><td colSpan={4} className="how">Nothing at the shore yet</td></tr>}
          {rows.map((s, i) => {
            const current = !rows.slice(i + 1).some(r => r.device === s.device)
            const gaps = s.max - s.min + 1 - s.count
            return (
              <tr key={`${s.device}:${s.bootId}`}>
                <td><b>{s.device.toUpperCase()}</b><small className="mono">{s.bootId.slice(0, 8)}… {current ? '' : '(previous boot)'}</small></td>
                <td className="num">{s.min}–{s.max}</td>
                <td className={`num ${gaps ? 'warn-text' : ''}`}>{gaps}</td>
                <td className="how">{current ? quality[s.device] : 'closed by reboot, not counted as loss'}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
      <p className="note">One seq counter per node, shared by its sensors, restarted with every new bootId. Gaps only count inside one boot.</p>
    </section>
  )
}
