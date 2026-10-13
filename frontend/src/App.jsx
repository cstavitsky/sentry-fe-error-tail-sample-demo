import React, { useEffect, useState } from 'react';
import { SCENARIOS, runScenario, runAll, runS6OnPageload } from './scenarios';

const JAEGER = import.meta.env.VITE_JAEGER_URL || 'http://localhost:16686';
const SENTRY_ORG = import.meta.env.VITE_SENTRY_ORG || '';

const styles = {
  page: { fontFamily: 'system-ui, sans-serif', maxWidth: 960, margin: '0 auto', padding: 16, color: '#1d1d1f' },
  grid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: 8 },
  btn: { textAlign: 'left', padding: '10px 12px', borderRadius: 8, border: '1px solid #ccc', background: '#fafafa', cursor: 'pointer' },
  table: { width: '100%', borderCollapse: 'collapse', marginTop: 16, fontSize: 13 },
  td: { borderTop: '1px solid #eee', padding: '6px 4px', verticalAlign: 'top', wordBreak: 'break-all' },
};

export default function App() {
  const [results, setResults] = useState([]);
  const [busy, setBusy] = useState(false);
  const add = r => setResults(prev => [r, ...prev.filter(p => p.traceId !== r.traceId)]);

  useEffect(() => {
    if (new URLSearchParams(window.location.search).get('run') === 'S6') runS6OnPageload(r => add({ ...r }));
  }, []);

  const run = async id => {
    setBusy(true);
    try { add(await runScenario(id)); } finally { setBusy(false); }
  };
  const all = async () => {
    setBusy(true);
    try { (await runAll()).forEach(add); } finally { setBusy(false); }
  };

  return (
    <div style={styles.page}>
      <h1>Tail sampling demo</h1>
      <p>Each button starts a new trace. All traces go to Jaeger; only traces with an error or a slow backend request
        (or the force-keep test flag) are forwarded to Sentry.</p>
      <div style={styles.grid}>
        {SCENARIOS.map(s => (
          <button key={s.id} data-scenario={s.id} style={styles.btn} disabled={busy} onClick={() => run(s.id)}>
            <b>{s.id}</b> {s.title}<br />
            <small>{s.expectSentry ? 'expected in Sentry' : 'NOT expected in Sentry'}</small>
          </button>
        ))}
        <button data-scenario="ALL" style={{ ...styles.btn, background: '#eef' }} disabled={busy} onClick={all}>
          <b>Run all</b> (S1–S9 except S6, which reloads the page)
        </button>
      </div>
      <table style={styles.table}>
        <thead><tr><th align="left">Scenario</th><th align="left">Trace</th><th align="left">HTTP</th></tr></thead>
        <tbody>
          {results.map(r => (
            <tr key={r.traceId} data-trace-id={r.traceId}>
              <td style={styles.td}>{r.scenario}{r.pending ? ' (error in 5s…)' : ''}</td>
              <td style={styles.td}>
                <code>{r.traceId}</code><br />
                <a href={`${JAEGER}/trace/${r.traceId}`} target="_blank" rel="noreferrer">Jaeger</a>
                {SENTRY_ORG && <> · <a href={`https://${SENTRY_ORG}.sentry.io/explore/traces/trace/${r.traceId}/`} target="_blank" rel="noreferrer">Sentry</a></>}
              </td>
              <td style={styles.td}>{r.calls.map(c => `${c.status} ${c.url}`).join(', ')}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
