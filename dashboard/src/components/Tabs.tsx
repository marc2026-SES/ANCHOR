export type TabId = 'overview' | 'sensors' | 'measurement' | 'ai' | 'security'

export const TABS: { id: TabId; label: string; hint: string }[] = [
  { id: 'overview', label: 'Overview', hint: 'Continuity, where the data is, events' },
  { id: 'sensors', label: 'Sensors', hint: 'BRIDGE, ENGINE and CARGO nodes, sensor quality' },
  { id: 'measurement', label: 'Measurement', hint: 'ANCHOR vs baseline: loss, delay, backlog drain, commands' },
  { id: 'ai', label: 'AI', hint: 'AI status, anomalies and forecasts from the ship (anchorAI)' },
  { id: 'security', label: 'Security', hint: 'ACPs as provisioned by bootstrap, denied-request tests, transport' },
]

export function Tabs({ active, onChange }: { active: TabId; onChange: (t: TabId) => void }) {
  return (
    <nav className="tabs" role="tablist" aria-label="Dashboard views">
      {TABS.map(t => (
        <button key={t.id} role="tab" aria-selected={active === t.id} title={t.hint}
          className={active === t.id ? 'on' : ''} onClick={() => onChange(t.id)}>
          {t.label}
        </button>
      ))}
    </nav>
  )
}
