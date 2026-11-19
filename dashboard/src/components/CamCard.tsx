// Deck camera: the ship Pi 5 camera (IMX219) MJPEG stream, relayed by the Shore Monitor as /cam.mjpg
// (one origin: no CORS, no second certificate to trust — ship-dashboard guide §10.4). MJPEG freezes
// silently when the link drops, so the stream is reopened when link A comes back and retried on error.
import { useEffect, useRef, useState } from 'react'

export function CamCard({ linkUp, live, height = 300 }: { linkUp: boolean; live: boolean; height?: number }) {
  const [nonce, setNonce] = useState(() => Date.now())
  const [err, setErr] = useState(false)
  const retry = useRef<number>()

  // reopen when the ship link comes back (a stalled MJPEG never fires onerror)
  useEffect(() => {
    if (live && linkUp) { setErr(false); setNonce(Date.now()) }
  }, [linkUp, live])
  useEffect(() => () => clearTimeout(retry.current), [])

  const onError = () => {
    setErr(true)
    clearTimeout(retry.current)
    retry.current = window.setTimeout(() => { setErr(false); setNonce(Date.now()) }, 3000)
  }

  return (
    <section className="panel cam-card">
      <header className="panel-head"><h3>Deck camera</h3>
        <span>ship Pi 5 IMX219, 960×720 15 fps, relayed by the Shore Monitor</span></header>
      <div className="cam-stage" style={{ height }}>
        {live && !err && linkUp && <img src={`/cam.mjpg?${nonce}`} alt="deck camera" onError={onError} />}
        {live && !linkUp && (
          <div className="cam-empty"><b>Ship video cut</b>
            <small>video is not stored, so this gap is gone — reconnects the moment link A returns</small></div>
        )}
        {live && err && linkUp && (
          <div className="cam-empty"><b>Camera unreachable</b>
            <small>retrying every 3 s, reconnects when link A returns</small></div>
        )}
        {!live && (
          <div className="cam-empty"><b>Live runs only</b>
            <small>the stream comes from the ship over link A, not from recorded data</small></div>
        )}
      </div>
    </section>
  )
}
