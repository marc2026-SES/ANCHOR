import type { Command } from '../types'
import type { Metrics } from '../lib/metrics'
import { commandStep, fmtMs, n } from '../lib/metrics'
import { clock } from '../theme'
import { useRolling } from '../lib/roll'

function Card({ label, value, sub, tone, bar }: { label: string; value: string; sub: string; tone?: 'good' | 'bad'; bar?: number }) {
  return (
    <div className={`kpi ${tone ?? ''}`}>
      <div className="kpi-label">{label}</div>
      <div className="kpi-value">{useRolling(value)}</div>
      {bar !== undefined && (
        <div className="kpi-bar" aria-hidden="true"><i style={{ width: `${Math.min(100, Math.max(0, bar))}%` }} /></div>
      )}
      <div className="kpi-sub" title={sub}>{sub}</div>
    </div>
  )
}

export function KpiCards({ m, command, now }: { m: Metrics; command?: Command; now: number }) {
  const drain = [...m.drains].reverse().find(d => d.items > 0)
  const pct = m.generated ? (m.shoreConfirmed / m.generated) * 100 : 0
  const step = command ? commandStep(command, now) : undefined
  return (
    <div className="kpis">
      <Card label="Shore confirmed" value={`${pct.toFixed(pct > 99 && pct < 100 ? 1 : 0)}%`} bar={pct}
        sub={`${n(m.shoreConfirmed)} of ${n(m.generated)}, coast ${n(m.coastCustody)}`} />
      <Card label="Records lost" value={n(m.lost)}
        sub={`seq gaps now ${n(m.seqGaps)}, ${m.reboots ? `${m.reboots} reboot` : 'no reboot'}`}
        tone={m.lost === 0 ? 'good' : 'bad'} />
      <Card label="Live delay (p50)" value={fmtMs(m.live.p50)}
        sub={`p95 ${fmtMs(m.live.p95)}, A ${fmtMs(m.hopA.p50)}, B ${fmtMs(m.hopB.p50)}`} />
      <Card label="Held data delay (p50)" value={fmtMs(m.held.p50)}
        sub={m.held.n ? `critical ${fmtMs(m.heldCritical.p50)}, max ${fmtMs(m.held.max)}` : 'nothing held yet'} />
      <Card label="Backlog drain"
        value={drain ? (drain.drained === null ? 'draining' : fmtMs(drain.drained)) : '—'}
        sub={drain ? `link ${drain.link} up ${clock(drain.up).slice(0, 5)}, ${n(drain.items)} records` : 'no backlog yet'}
        tone={drain && drain.drained !== null ? 'good' : undefined} />
      <Card label="Shore command" value={step ?? '—'}
        sub={!command ? 'none' : command.appliedAt !== null && command.appliedAt <= now
          ? `issued → applied ${fmtMs(command.appliedAt - command.issuedAt)}`
          : `${command.commandId}, TTL ${fmtMs(command.expiresAt - command.issuedAt)}`}
        tone={step === 'applied' ? 'good' : step === 'expired' ? 'bad' : undefined} />
    </div>
  )
}
