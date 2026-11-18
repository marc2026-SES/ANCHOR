import { ACPS, DENY_TESTS, TRANSPORT, type DenyResult, type DenyRun } from '../security/acp'
import denyRun from '../security/deny-results.json'

const RUN = denyRun as DenyRun
const RESULTS = new Map(RUN.results.map(r => [r.id, r]))

function ResultPill({ r }: { r: DenyResult | undefined }) {
  if (!r) return <span className="pill pending">not run</span>
  const label = r.status === 'pass' ? `pass (${r.got})` : r.status === 'fail' ? `fail (got ${r.got})` : r.status
  return (
    <>
      <span className={`pill ${r.status}`} title={r.note || undefined}>{label}</span>
      {r.note && r.status !== 'pass' && <small className="result-note">{r.note}</small>}
    </>
  )
}

function ranLine(run: DenyRun) {
  if (!run.ranAt) return 'not run yet (server/deny_tests.py)'
  const at = new Date(run.ranAt).toISOString().slice(0, 16).replace('T', ' ')
  const passed = run.results.filter(r => r.status === 'pass').length
  return `${passed}/${run.results.length} as expected at ${at} UTC (${Object.values(run.cses).join(', ')})`
}

const OPS: Record<string, string> = { C: 'create', 'C(SUB)': 'create subscription only: acod/chty=[23]', 'C(CIN)': 'create content instance only: acod/chty=[4]', R: 'retrieve', U: 'update', D: 'delete', N: 'notify', Di: 'discover', all: 'all operations (63)' }

export function SecurityTab() {
  return (
    <div className="tab-body security">
      <section className="panel">
        <header className="panel-head"><h3>Access control (ACP)</h3><span>one ACP per resource, named acp-&lt;resource path&gt;. ★ = added to the current bootstrap. pvs: CAdmin only</span></header>
        <div className="table-wrap">
          <table className="grid acp">
            <thead><tr><th>ACP</th><th>Applied to</th><th>CSE</th><th>Who may do what</th></tr></thead>
            <tbody>
              {ACPS.map((a, i) => (
                <tr key={i}>
                  <td className="mono">{a.name}</td>
                  <td>{a.on}</td>
                  <td>{a.cse}</td>
                  <td>
                    {a.grants.map(g => (
                      <div key={g.who} className="grant">
                        <span className="who">{g.who}</span>
                        {g.ops.split(' ').map(o => <span key={o} className="op" title={OPS[o]}>{o}</span>)}
                      </div>
                    ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="note">C create, C(SUB) create subscription only, C(CIN) create content instance only, R retrieve, U update, D delete, N notify, Di discover. Each conditional grant permits only its named child type; anything not listed is denied (4103).</p>
      </section>

      <div className="row two">
        <section className="panel">
          <header className="panel-head"><h3>Access-control tests</h3><span>T8, T12 and T16 must pass, the rest must be refused. {ranLine(RUN)}</span></header>
          <table className="grid">
            <thead><tr><th>#</th><th>Test</th><th>Request</th><th>Expected</th><th>Result</th></tr></thead>
            <tbody>
              {DENY_TESTS.map(t => (
                <tr key={t.id}>
                  <td>{t.id}</td>
                  <td><b>{t.test}</b><small>{t.who}</small></td>
                  <td className="mono">{t.request}</td>
                  <td>{t.expect}</td>
                  <td><ResultPill r={RESULTS.get(t.id)} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
        <section className="panel">
          <header className="panel-head"><h3>Transport and privacy</h3><span>who sent it, before what they may do</span></header>
          <ul className="plain">
            {TRANSPORT.map(t => (
              <li key={t.hop}><b>{t.hop}{t.open && <span className="pill open">open</span>}</b><span>{t.how}</span></li>
            ))}
          </ul>
        </section>
      </div>
    </div>
  )
}
