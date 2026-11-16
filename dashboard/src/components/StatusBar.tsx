import type { LinkChange, LinkId } from '../types'
import { C, LINK, clock } from '../theme'
import { fmtMs, linkUpAt, n } from '../lib/metrics'

function LinkPill({ id, up }: { id: LinkId; up: boolean }) {
  return (
    <div className="link-pill" style={{ background: up ? C.up : C.down }} title={LINK[id].name}>
      <small>{LINK[id].short}</small>{up ? 'up' : 'down'}
    </div>
  )
}

export function StatusBar(props: {
  runId: string; links: LinkChange[]; now: number; lastAtShore: number | null; heldOnShip: number; waitingAtCoast: number
  mode: 'anchor' | 'baseline'; onMode: (m: 'anchor' | 'baseline') => void
}) {
  const { runId, links, now, lastAtShore, heldOnShip, waitingAtCoast, mode, onMode } = props
  const silence = lastAtShore === null ? null : now - lastAtShore
  return (
    <header className="status">
      <div className="brand">
        <strong>ANCHOR</strong>
        <span>Shore dashboard · {runId}</span>
      </div>
      <LinkPill id="A" up={linkUpAt(links, 'A', now)} />
      <LinkPill id="B" up={linkUpAt(links, 'B', now)} />
      <div className="stat"><span>Last record at shore</span><b className={silence !== null && silence > 10_000 ? 'warn' : ''}>{fmtMs(silence)} ago</b></div>
      <div className="stat"><span>Held on ship</span><b>{n(heldOnShip)}</b></div>
      <div className="stat"><span>Waiting at coast</span><b>{n(waitingAtCoast)}</b></div>
      <div className="stat"><span>Time (UTC)</span><b>{clock(now)}</b></div>
      <div className="mode" role="group" aria-label="Mode">
        <button className={mode === 'anchor' ? 'on' : ''} onClick={() => onMode('anchor')}>ANCHOR</button>
        <button className={mode === 'baseline' ? 'on' : ''} onClick={() => onMode('baseline')}>Baseline</button>
      </div>
    </header>
  )
}

export function Playback(props: {
  start: number; end: number; now: number; playing: boolean; speed: number
  onSeek: (ms: number) => void; onPlay: (p: boolean) => void; onSpeed: (s: number) => void
}) {
  const { start, end, now, playing, speed, onSeek, onPlay, onSpeed } = props
  return (
    <div className="playback">
      <button className="play" onClick={() => { if (!playing && now >= end) onSeek(start); onPlay(!playing) }}>
        {playing ? 'Pause' : 'Play'}
      </button>
      <input type="range" min={start} max={end} step={1000} value={now} onChange={e => onSeek(Number(e.target.value))}
        aria-label="Playback position" />
      <select value={speed} onChange={e => onSpeed(Number(e.target.value))} aria-label="Playback speed">
        {[1, 10, 30, 60].map(s => <option key={s} value={s}>{s}×</option>)}
      </select>
      <span className="hint">dummy data · demo run in the README shooting order (A and B cut separately)</span>
    </div>
  )
}
