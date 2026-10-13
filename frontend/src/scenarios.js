// Demo scenarios S1–S9. Each button click starts a NEW trace so every scenario can be
// found (and sampled) independently. Exposed as window.__demo for the Playwright loadgen.
import { Sentry } from './sentry';

const FORCE_KEEP_HEADER = 'x-demo-force-keep';

/** Start a fresh trace with a root span for one scenario run. */
function startScenarioSpan(id, title, { forceKeep = false } = {}) {
  return Sentry.startNewTrace(() =>
    Sentry.startInactiveSpan({
      name: `scenario ${id}: ${title}`,
      op: 'ui.action.demo',
      forceTransaction: true,
      attributes: {
        'demo.scenario': id,
        // String, because the collector's string_attribute policy matches strings only.
        ...(forceKeep && { 'demo.force_keep': 'true' }),
      },
    }),
  );
}

/**
 * fetch() inside the scenario span. The browser has no async context, so we make the
 * span active only for the synchronous fetch() call; that's when Sentry's fetch
 * instrumentation creates the http.client child span and adds traceparent headers.
 */
async function api(span, url, { method = 'GET', body, forceKeep = false } = {}) {
  const headers = { ...(body && { 'content-type': 'application/json' }), ...(forceKeep && { [FORCE_KEEP_HEADER]: '1' }) };
  const res = await Sentry.withActiveSpan(span, () =>
    fetch(url, { method, headers, body: body ? JSON.stringify(body) : undefined }),
  );
  const json = await res.json().catch(() => null);
  return { url, status: res.status, json };
}

function renderUtilizationChart(replicaResponse) {
  // Deliberate bug for S5: `utilization` is not part of the API response.
  return replicaResponse.utilization.map(u => `${u.replica}: ${u.pct}%`);
}

export const SCENARIOS = [
  { id: 'S1', title: 'Happy path (fast 200s)', expectSentry: false, errors: { fe: 0, be: 0 } },
  { id: 'S2', title: 'Backend unhandled exception on /graphql/ (500)', expectSentry: true, errors: { fe: 0, be: 1 } },
  { id: 'S3', title: 'Backend handled exception (200)', expectSentry: true, errors: { fe: 0, be: 1 } },
  { id: 'S4', title: 'Go service error, Django returns 502', expectSentry: true, errors: { fe: 0, be: 2 } },
  { id: 'S5', title: 'Frontend JS error after successful API calls', expectSentry: true, errors: { fe: 1, be: 0 } },
  { id: 'S6', title: 'Frontend error ~5s after page-load span ended (reloads page)', expectSentry: true, errors: { fe: 1, be: 0 } },
  { id: 'S7', title: 'Slow backend (third-party billing call), no error', expectSentry: true, errors: { fe: 0, be: 0 } },
  { id: 'S8', title: 'N+1 DB queries (force-keep)', expectSentry: true, errors: { fe: 0, be: 0 }, forceKeep: true },
  { id: 'S9', title: 'Slow DB query (force-keep)', expectSentry: true, errors: { fe: 0, be: 0 }, forceKeep: true },
];

const RUNNERS = {
  async S1(span) {
    return [await api(span, '/api/v1/models'), await api(span, '/api/v1/models/1/deployments')];
  },
  async S2(span) {
    return [
      await api(span, '/api/v1/models'),
      await api(span, '/graphql/', {
        method: 'POST',
        body: { operationName: 'ModelReplicaDetails', query: 'query ModelReplicaDetails($modelId: ID!) { replicaDetails(modelId: $modelId) { id region } }', variables: { modelId: 1 } },
      }),
    ];
  },
  async S3(span) {
    return [await api(span, '/api/v1/models/3/config')];
  },
  async S4(span) {
    return [await api(span, '/api/v1/models/2/deployments?replica_fail=1')];
  },
  async S5(span) {
    const calls = [await api(span, '/api/v1/models'), await api(span, '/api/v1/models/1/deployments')];
    try {
      renderUtilizationChart(calls[1].json?.replica ?? {});
    } catch (err) {
      // Captured while the scenario span is active -> error carries this trace.
      Sentry.withActiveSpan(span, () => {
        Sentry.logger.error('Failed to render utilization chart', { scenario: 'S5' });
        Sentry.captureException(err);
      });
    }
    return calls;
  },
  async S7(span) {
    return [await api(span, '/api/settings/billing')];
  },
  async S8(span) {
    return [await api(span, '/api/v1/models/1/deployment-history', { forceKeep: true })];
  },
  async S9(span) {
    return [await api(span, '/api/v1/deployments/status-report', { forceKeep: true })];
  },
};

const results = [];

/** Run one scenario; resolves with { scenario, traceId, calls, ... }. */
export async function runScenario(id) {
  if (id === 'S6') {
    // S6 needs a fresh page load; see runS6OnPageload().
    window.location.assign('/?run=S6');
    return new Promise(() => {});
  }
  const meta = SCENARIOS.find(s => s.id === id);
  if (!meta || !RUNNERS[id]) throw new Error(`unknown scenario ${id}`);
  const span = startScenarioSpan(id, meta.title, { forceKeep: !!meta.forceKeep });
  const traceId = span.spanContext().traceId;
  const startedAt = new Date().toISOString();
  let calls = [];
  try {
    calls = await RUNNERS[id](span);
  } finally {
    span.end(); // segment end -> span buffer flushes ~500 ms later
  }
  const result = { scenario: id, traceId, startedAt, calls: calls.map(c => ({ url: c.url, status: c.status })), expectSentry: meta.expectSentry, errors: meta.errors };
  results.push(result);
  return result;
}

export async function runAll() {
  const out = [];
  for (const s of SCENARIOS) if (s.id !== 'S6') out.push(await runScenario(s.id));
  return out;
}

/**
 * S6: on `/?run=S6`, the page-load span fetches /api/v1/models; after the page-load
 * span has ended we wait 5 s and throw an *unhandled* error. With no active span the
 * error event takes the page-load trace id from the scope's propagation context, and
 * the transport's error-marker span tells the collector to keep that trace.
 */
export function runS6OnPageload(onResult) {
  const client = Sentry.getClient();
  let done = false;
  fetch('/api/v1/models').catch(() => {}); // child of the page-load span
  client?.on('spanEnd', span => {
    if (done || span !== Sentry.getRootSpan(span)) return;
    const json = Sentry.spanToJSON(span);
    if (json.op !== 'pageload') return;
    done = true;
    const traceId = span.spanContext().traceId;
    const result = { scenario: 'S6', traceId, startedAt: new Date().toISOString(), calls: [{ url: '/api/v1/models', status: 200 }], expectSentry: true, errors: { fe: 1, be: 0 }, pending: true };
    results.push(result);
    onResult?.(result);
    setTimeout(() => {
      result.pending = false;
      result.errorAt = new Date().toISOString();
      onResult?.(result);
      throw new Error('S6: late error after page-load span ended (stale replica cache)');
    }, 5000);
  });
}

if (typeof window !== 'undefined') {
  window.__demo = { SCENARIOS, run: runScenario, runAll, results, flush: (ms = 5000) => Sentry.flush(ms) };
}
