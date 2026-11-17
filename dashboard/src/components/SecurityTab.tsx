import { ACPS, DENY_TESTS, TRANSPORT } from '../security/acp'

const OPS: Record<string, string> = { C: 'create', 'C(SUB)': 'create subscription only: acod/chty=[23]', R: 'retrieve', U: 'update', D: 'delete', N: 'notify', Di: 'discover', all: 'all operations (63)' }

export function SecurityTab() {
  return (
    <div className="tab-body security">
      <section className="panel">
        <header className="panel-head"><h3>Access control (ACP)</h3><span>one ACP per resource, named acp-&lt;resource path&gt; · ★ = added to the current bootstrap · pvs: CAdmin only</span></header>
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
        <p className="note">C create · C(SUB) create subscription only · R retrieve · U update · D delete · N notify · Di discover. Conditional create does not permit CIN writes.</p>
      </section>

      <div className="row two">
        <section className="panel">
          <header className="panel-head"><h3>Access-control tests</h3><span>T8 and T12 must pass · all other requests must be refused</span></header>
          <table className="grid">
            <thead><tr><th>#</th><th>Test</th><th>Request</th><th>Expected</th><th>Result</th></tr></thead>
            <tbody>
              {DENY_TESTS.map(t => (
                <tr key={t.id}>
                  <td>{t.id}</td>
                  <td><b>{t.test}</b><small>{t.who}</small></td>
                  <td className="mono">{t.request}</td>
                  <td>{t.expect}</td>
                  <td><span className="pill pending">not run</span></td>
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
