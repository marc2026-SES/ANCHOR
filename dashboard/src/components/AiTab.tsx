// AI tab and alert strip. Data: the anchorAI resources (the shore CSE's copy, else the ship CSE), read by
// the Shore Monitor as CAnchorDashboard (live), or demo samples. Field meanings follow the AI spec ("AI → 대시보드 연동 스펙").
import type { AiAnomaly, AiInfo, AiLevel, AiPrognosis, AiState } from '../types'
import { clock } from '../theme'
import { fmtMs } from '../lib/metrics'

const STATE: Record<AiState, { label: string; tone: 'ok' | 'warn' | 'bad' | 'off'; hint: string }> = {
  ok: { label: 'AI online', tone: 'ok', hint: 'status heartbeat arriving' },
  stale: { label: 'AI not responding', tone: 'warn', hint: 'no new status from the AI' },
  no_ai: { label: 'AI not running', tone: 'off', hint: 'anchorAI is on neither the shore nor the ship CSE yet' },
  forbidden: { label: 'No permission', tone: 'bad', hint: 'ACP: CAnchorDashboard may not read anchorAI' },
  ship_unreachable: { label: 'Ship not reachable', tone: 'off',
    hint: 'no new AI results reach the shore while the ship is cut off (link A); they show when it is back · this is not an AI failure' },
  error: { label: 'Read error', tone: 'bad', hint: 'a CSE answered with an error' },
  starting: { label: 'Checking…', tone: 'off', hint: 'first read pending' },
}

const CONTAINER: Record<string, string> = { vibration: 'Vibration', gas: 'Gas MQ-2', attitude: 'Attitude', climate: 'Climate', hatch: 'Hatch' }
const VALUE: Record<string, string> = {
  vib_rms_g: 'RMS', vib_peak_g: 'peak', mq2_v: 'voltage', roll: 'roll', pitch: 'pitch', temp_c: 'temperature', rh: 'humidity',
}
const STAT: Record<string, string> = { mean: 'level', std: 'spread', slope: 'trend', max: 'max', min: 'min' }

/** "vibration.vib_rms_g.std" → "Vibration RMS · spread" (raw name in the tooltip) */
export function featureLabel(feature: string) {
  const [c, v, s] = feature.split('.')
  if (!c || !v || !s) return feature
  return `${CONTAINER[c] ?? c} ${VALUE[v] ?? v} · ${STAT[s] ?? s}`
}

/** "ENGINE/gas.mq2_v" → "ENGINE gas voltage" */
function channelLabel(channel: string) {
  const [sub, rest = ''] = channel.split('/')
  const [c, v] = rest.split('.')
  return `${sub} ${CONTAINER[c] ?? c ?? ''} ${VALUE[v] ?? v ?? ''}`.trim()
}

const mmss = (ms: number) => {
  const s = Math.max(0, Math.round(ms / 1000))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

function Level({ level }: { level: AiLevel | string }) {
  return <span className={`pill ai-${level}`}>{level}</span>
}

/** prognoses whose predicted time is still ahead, newest first */
const activePrognoses = (ai: AiInfo, now: number) =>
  ai.prognosis.filter(p => p.at <= now && p.at + p.etaS * 1000 > now)

const recentCritical = (ai: AiInfo, now: number) =>
  ai.anomaly.filter(a => a.level === 'critical' && a.at <= now && now - a.at < 120_000)

/** strip above the tabs: a predicted threshold still ahead, or a critical anomaly in the last 2 minutes */
export function AiAlerts({ ai, now, onOpen }: { ai: AiInfo | null | undefined; now: number; onOpen: () => void }) {
  if (!ai) return null
  const prog = activePrognoses(ai, now)
  const crit = recentCritical(ai, now)
  if (!prog.length && !crit.length) return null
  return (
    <div className="ai-alerts" role="status">
      {prog.map(p => (
        <button key={p.eventId} className="ai-alert prog" onClick={onOpen}>
          <b>AI forecast</b> {channelLabel(p.channel)} reaches {p.threshold ?? 'its limit'} in <b>{mmss(p.at + p.etaS * 1000 - now)}</b> at this trend
          {p.slopePerMin !== undefined && <small> ({p.slopePerMin > 0 ? '+' : ''}{p.slopePerMin}/min, now {p.value})</small>}
        </button>
      ))}
      {crit.map(a => (
        <button key={a.eventId} className="ai-alert crit" onClick={onOpen}>
          <b>AI anomaly</b> {a.subsystem} critical · score {a.score.toFixed(2)}
          {a.topFeatures?.[0] && <small> · {featureLabel(a.topFeatures[0].feature)}</small>} · {fmtMs(now - a.at)} ago
        </button>
      ))}
    </div>
  )
}

function StatusCard({ ai, now, live }: { ai: AiInfo; now: number; live: boolean }) {
  const st = STATE[ai.state] ?? STATE.error
  const age = ai.status?.seenAt ? now - ai.status.seenAt : null
  return (
    <section className="panel ai-status">
      <header className="panel-head"><h3>AI status</h3><span>{live
        ? `${ai.source === 'ship' ? 'ship CSE anchorAI' : ai.source === 'shore' ? 'shore CSE copy of anchorAI' : 'anchorAI'} · aiStatus every 30 s`
        : 'demo data'}</span></header>
      <div className={`ai-state ${st.tone}`} title={ai.error ?? st.hint}>
        <b>{st.label}</b><small>{ai.error ?? st.hint}</small>
      </div>
      <div className="ai-levels">
        {['ENGINE', 'BRIDGE', 'CARGO'].map(sub => {
          const level = ai.status?.levels?.[sub]
          return (
            <div key={sub} className={`ai-level ${ai.state === 'ok' && level ? level : 'unknown'}`}>
              <small>{sub}</small><b>{ai.state === 'ok' && level ? level : '—'}</b>
            </div>
          )
        })}
      </div>
      <dl className="ai-facts">
        <div><dt>Windows analysed</dt><dd>{ai.status?.windows?.toLocaleString('en-US') ?? '—'}</dd></div>
        <div><dt>Siren</dt><dd>{ai.status?.sirenOn === undefined ? '—' : ai.status.sirenOn ? 'ON' : 'off'}</dd></div>
        <div><dt>Last status</dt><dd>{age === null ? '—' : `${fmtMs(age)} ago`}</dd></div>
      </dl>
    </section>
  )
}

function AnomalyTable({ rows, now }: { rows: AiAnomaly[]; now: number }) {
  return (
    <section className="panel">
      <header className="panel-head"><h3>Anomalies</h3><span>created only when the AI sees one · top features = why (larger |z| = stronger)</span></header>
      {rows.length === 0 ? <p className="empty">No anomaly in this run.</p> : (
        <table className="ai-table">
          <thead><tr><th>Time (UTC)</th><th>Subsystem</th><th>Level</th><th>Score</th><th>Why</th></tr></thead>
          <tbody>
            {rows.map(a => (
              <tr key={a.eventId}>
                <td>{clock(a.at)}<small>{fmtMs(now - a.at)} ago</small></td>
                <td>{a.subsystem}</td>
                <td><Level level={a.level} /></td>
                <td><div className="ai-score"><i style={{ width: `${Math.round(Math.min(1, Math.max(0, a.score)) * 100)}%` }}
                  className={a.level} /><span>{a.score.toFixed(2)}</span></div></td>
                <td>{(a.topFeatures ?? []).slice(0, 3).map(f => (
                  <span key={f.feature} className="ai-feature" title={f.feature}>{featureLabel(f.feature)} <em>z {f.z.toFixed(1)}</em></span>
                ))}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}

function Forecasts({ ai, now }: { ai: AiInfo; now: number }) {
  const prog = ai.prognosis.filter(p => p.at <= now)
  const links = ai.linkForecast.filter(l => l.at <= now)
  return (
    <section className="panel">
      <header className="panel-head"><h3>Forecasts</h3><span>threshold before it is crossed · link loss before it happens (GPS fix only)</span></header>
      {prog.length === 0 && links.length === 0 && <p className="empty">No forecast in this run.</p>}
      <ul className="ai-list">
        {prog.map((p: AiPrognosis) => {
          const left = p.at + p.etaS * 1000 - now
          return (
            <li key={p.eventId}>
              <span className={`pill ${left > 0 ? 'ai-warn' : 'ai-normal'}`}>{left > 0 ? `in ${mmss(left)}` : 'time passed'}</span>
              {channelLabel(p.channel)} → {p.threshold ?? 'limit'} <small>(now {p.value ?? '?'}, {p.slopePerMin ?? '?'}/min · {clock(p.at)})</small>
            </li>
          )
        })}
        {links.map(l => (
          <li key={l.eventId}>
            <span className="pill ai-link">{l.type.replace('_', ' ')}</span>
            {l.mode?.toUpperCase()} {l.etaS !== undefined && l.type === 'loss_predicted' ? `loss expected ${mmss(l.etaS * 1000)} after ${clock(l.at)}` : clock(l.at)}
            {l.routeMode && <small> · {l.routeMode}</small>}
          </li>
        ))}
      </ul>
    </section>
  )
}

function SummaryTable({ ai }: { ai: AiInfo }) {
  const stats = Object.entries(ai.summary?.stats ?? {})
  return (
    <section className="panel">
      <header className="panel-head"><h3>What the AI is looking at</h3><span>last 1-minute summary per sensor</span></header>
      {stats.length === 0 ? <p className="empty">No summary yet.</p> : (
        <table className="ai-table">
          <thead><tr><th>Sensor</th><th>Mean</th><th>Min</th><th>Max</th><th>n</th></tr></thead>
          <tbody>
            {stats.map(([key, v]) => (
              <tr key={key}><td>{channelLabel(key)}</td><td>{v.mean}</td><td>{v.min}</td><td>{v.max}</td><td>{v.n}</td></tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}

export function AiTab({ ai, now, live }: { ai: AiInfo | null | undefined; now: number; live: boolean }) {
  if (!ai) {
    return (
      <div className="tab-body ai">
        <section className="panel"><header className="panel-head"><h3>AI</h3></header>
          <p className="empty">The Shore Monitor does not read the AI: set ai.enabled (and shore.url or ship.url) in its config.</p></section>
      </div>
    )
  }
  return (
    <div className="tab-body ai">
      <div className="ai-top">
        <StatusCard ai={ai} now={now} live={live} />
        <Forecasts ai={ai} now={now} />
      </div>
      <AnomalyTable rows={ai.anomaly.filter(a => a.at <= now)} now={now} />
      <SummaryTable ai={ai} />
    </div>
  )
}
