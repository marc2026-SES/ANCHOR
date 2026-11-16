import type { FeedItem, FeedKind } from '../types'
import { clock } from '../theme'

const KIND: Record<FeedKind, string> = {
  link: 'Link', incident: 'Incident', command: 'Command', delivery: 'Delivery', security: 'Security',
}

export function EventLog({ feed, now }: { feed: FeedItem[]; now: number }) {
  const shown = feed
    .filter(e => e.t <= now)
    .sort((a, b) => b.t - a.t)
  return (
    <section className="panel events">
      <header className="panel-head"><h3>Events</h3><span>newest first · when it happened · when the shore saw it</span></header>
      <ul>
        {shown.length === 0 && <li className="empty">No events yet</li>}
        {shown.map((e, i) => (
          <li key={i}>
            <span className={`tag ${e.kind}`}>{KIND[e.kind]}</span>
            <span className="ev-time">{clock(e.t)}</span>
            <span className="ev-text">{e.text}
              {e.seen !== undefined && e.seen - e.t > 10_000 && (
                <em> · {e.seen <= now ? `at shore ${clock(e.seen)}` : 'not at shore yet'}</em>)}
            </span>
          </li>
        ))}
      </ul>
    </section>
  )
}
