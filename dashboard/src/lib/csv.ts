// CSV export of a run (columns follow the measurement record L1 in Data_Spec.md, adapted to the core envelope).
import type { Delivered, LinkChange } from '../types'

const iso = (ms: number | null) => (ms === null ? '' : new Date(ms).toISOString())

function download(name: string, rows: (string | number)[][]) {
  const text = rows.map(r => r.map(v => {
    const s = String(v)
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
  }).join(',')).join('\n')
  const url = URL.createObjectURL(new Blob([text + '\n'], { type: 'text/csv' }))
  const a = document.createElement('a')
  a.href = url
  a.download = name
  a.click()
  URL.revokeObjectURL(url)
}

export function exportEvents(runId: string, mode: string, es: Delivered[]) {
  download(`${runId}_events.csv`, [
    ['run_id', 'mode', 'event_id', 'device_id', 'sensor', 'kind', 'priority', 'boot_id', 'seq', 'generated_at', 'coast_at', 'shore_at'],
    ...es.map(e => [runId, mode, e.eventId, e.deviceId, e.sensor ?? '', e.kind, e.priority, e.bootId, e.seq ?? '',
      iso(e.generatedAt), iso(e.coastAt), iso(e.shoreAt)]),
  ])
}

export function exportLinks(runId: string, ls: LinkChange[]) {
  download(`${runId}_links.csv`, [
    ['run_id', 't', 'link', 'state'],
    ...ls.map(l => [runId, iso(l.t), l.link, l.up ? 'up' : 'down']),
  ])
}
