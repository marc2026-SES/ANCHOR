import type { LinkId, LinkState } from '../types'
import { C, LINK, clock } from '../theme'
import { fmtMs, n } from '../lib/metrics'

function LinkPill({ id, state, inferred }: { id: LinkId; state: LinkState; inferred?: boolean }) {
  const bg = state === 'up' ? C.up : state === 'down' ? C.down : C.muted
  const tip = state === 'unknown' ? `${LINK[id].name}: no fresh report from the core that uses this link`
    : inferred ? `${LINK[id].name}: inferred from data arrivals (the core cannot be reached), accurate to a few seconds`
    : LINK[id].name
  return (
    <div className={`link-pill ${state}`} style={{ background: bg }} title={tip}>
      <small>{LINK[id].short}</small>{state === 'unknown' ? 'no report' : state}{inferred && state !== 'unknown' && <em className="inferred">inferred</em>}
    </div>
  )
}

export function StatusBar(props: {
  runId: string; link: Record<LinkId, LinkState>; inferred?: Record<LinkId, boolean>; now: number; lastAtShore: number | null; heldOnShip: number | null
  waitingAtCoast: number | null; mode: 'anchor' | 'baseline'; onMode?: (m: 'anchor' | 'baseline') => void
}) {
  const { runId, link, now, lastAtShore, heldOnShip, waitingAtCoast, mode, onMode } = props
  const silence = lastAtShore === null ? null : now - lastAtShore
  return (
    <header className="status">
      <div className="brand">
        <svg className="brand-mark" viewBox="0 0 24 24" aria-hidden="true">
          <circle cx="12" cy="5" r="2.1" fill="none" />
          <line x1="12" y1="7.2" x2="12" y2="18.4" />
          <line x1="8.4" y1="9.6" x2="15.6" y2="9.6" />
          <path d="M 4.6 13.6 Q 5.6 19.4 12 19.6 Q 18.4 19.4 19.4 13.6 L 16.9 15.0" fill="none" />
          <path d="M 4.6 13.6 L 7.1 15.0" fill="none" />
        </svg>
        <div>
          <strong>ANCHOR</strong>
          <span>Shore dashboard · {runId}</span>
        </div>
      </div>
      <LinkPill id="A" state={link.A} inferred={props.inferred?.A} />
      <LinkPill id="B" state={link.B} inferred={props.inferred?.B} />
      <div className="stat"><span>Last record at shore</span><b className={silence !== null && silence > 10_000 ? 'warn' : ''}>{fmtMs(silence)} ago</b></div>
      <div className="stat"><span>Held on ship</span><b>{heldOnShip === null ? '—' : n(heldOnShip)}</b></div>
      <div className="stat"><span>Waiting at coast</span><b>{waitingAtCoast === null ? '—' : n(waitingAtCoast)}</b></div>
      <div className="stat"><span>Time (UTC)</span><b>{clock(now)}</b></div>
      {onMode ? (
        <div className="mode" role="group" aria-label="Mode">
          <button className={mode === 'anchor' ? 'on' : ''} onClick={() => onMode('anchor')}>ANCHOR</button>
          <button className={mode === 'baseline' ? 'on' : ''} onClick={() => onMode('baseline')}>Baseline</button>
        </div>
      ) : (
        <div className="live-badge" title="Data from the Shore Monitor AE"><i />LIVE · {mode === 'anchor' ? 'ANCHOR' : 'Baseline'}</div>
      )}
    </header>
  )
}

export function Playback(props: {
  start: number; end: number; now: number; playing: boolean; speed: number
  onSeek: (ms: number) => void; onPlay: (p: boolean) => void; onSpeed: (s: number) => void
  /** live mode: the button returns to following the newest data instead of playing */
  live?: { following: boolean; onFollow: () => void; hint: string }
}) {
  const { start, end, now, playing, speed, onSeek, onPlay, onSpeed, live } = props
  return (
    <div className="playback">
      {live ? (
        <button className={`play ${live.following ? 'following' : ''}`} onClick={live.onFollow} disabled={live.following}>
          {live.following ? 'Following' : 'Back to live'}
        </button>
      ) : (
        <button className="play" onClick={() => { if (!playing && now >= end) onSeek(start); onPlay(!playing) }}>
          {playing ? 'Pause' : 'Play'}
        </button>
      )}
      <input type="range" min={start} max={end} step={1000} value={Math.min(now, end)} onChange={e => onSeek(Number(e.target.value))}
        aria-label="Playback position" />
      {!live && (
        <select value={speed} onChange={e => onSpeed(Number(e.target.value))} aria-label="Playback speed">
          {[1, 10, 30, 60].map(s => <option key={s} value={s}>{s}×</option>)}
        </select>
      )}
      <span className="hint">{live ? live.hint : 'dummy data · demo run in the README shooting order (A and B cut separately)'}</span>
    </div>
  )
}
