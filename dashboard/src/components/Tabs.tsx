export type TabId = 'overview' | 'sensors' | 'measurement' | 'ai' | 'security'

export const TABS: { id: TabId; label: string; hint: string; icon: JSX.Element }[] = [
  { id: 'overview', label: 'Overview', hint: 'Continuity, where the data is, events',
    icon: <><circle cx="8" cy="8" r="6" /><path d="M 8 8 L 12.2 3.8" /><circle cx="8" cy="8" r="1.4" className="fill" /></> },
  { id: 'sensors', label: 'Sensors', hint: 'BRIDGE, ENGINE and CARGO nodes, sensor quality',
    icon: <path d="M 1.5 9 L 4.5 9 L 6 4.5 L 8.5 12 L 10.5 7 L 11.6 9 L 14.5 9" /> },
  { id: 'measurement', label: 'Measurement', hint: 'ANCHOR vs baseline: loss, delay, backlog drain, commands',
    icon: <><path d="M 2 13 L 2 3" /><path d="M 2 13 L 14 13" /><path d="M 4.5 10.5 V 13 M 7.5 7.5 V 13 M 10.5 9 V 13 M 13.5 5 V 13" /></> },
  { id: 'ai', label: 'AI', hint: 'AI status, anomalies and forecasts (ship anchorAI, coastal fleetAI)',
    icon: <><rect x="3" y="3" width="10" height="10" rx="2.5" /><path d="M 6 1.5 V 3 M 10 1.5 V 3 M 6 13 V 14.5 M 10 13 V 14.5 M 1.5 6 H 3 M 1.5 10 H 3 M 13 6 H 14.5 M 13 10 H 14.5" /><circle cx="8" cy="8" r="1.6" className="fill" /></> },
  { id: 'security', label: 'Security', hint: 'ACPs as provisioned by bootstrap, access-control tests, transport',
    icon: <><path d="M 8 1.8 L 13.5 4 V 8 C 13.5 11.5 11 13.6 8 14.6 C 5 13.6 2.5 11.5 2.5 8 V 4 Z" /><path d="M 5.8 8.2 L 7.4 9.8 L 10.4 6.4" /></> },
]

export function Tabs({ active, onChange }: { active: TabId; onChange: (t: TabId) => void }) {
  return (
    <nav className="tabs" role="tablist" aria-label="Dashboard views">
      {TABS.map(t => (
        <button key={t.id} role="tab" aria-selected={active === t.id} title={t.hint}
          className={active === t.id ? 'on' : ''} onClick={() => onChange(t.id)}>
          <svg viewBox="0 0 16 16" aria-hidden="true">{t.icon}</svg>
          <span>{t.label}</span>
        </button>
      ))}
    </nav>
  )
}
