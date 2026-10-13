#!/usr/bin/env node
/**
 * Node harness for frontend/src/otlpSplitTransport.js (no browser, no Docker).
 * Needs tools/mock_sink.py running (default http://127.0.0.1:9999) and
 * `npm install` done in frontend/.
 *
 *   node tools/transport_harness.mjs [--sink http://127.0.0.1:9999]
 *
 * Part A ("real SDK"): runs the real @sentry/react 10.65.0 client in Node with
 *   spanStreamingIntegration + the split transport: a root span with two children,
 *   an error captured inside a span, an error captured with no active span, and a
 *   Sentry log. Default (browser) integrations are disabled because Node has no DOM.
 * Part B ("hand-made envelopes"): feeds edge cases straight into transport.send():
 *   a v1 standalone span item, a malformed span item, a span-only envelope (checks
 *   the {statusCode: 200} response), and a > 64 KiB batch (checks chunking).
 *
 * Prints a JSON summary of what the transport exported (via onExport) and the
 * trace ids used, so they can be reconciled against out/mock_sink/*.jsonl.
 */
import * as Sentry from '../frontend/node_modules/@sentry/react/build/esm/index.js';
import { makeOtlpSplitTransport } from '../frontend/src/otlpSplitTransport.js';

const sinkArg = process.argv.indexOf('--sink');
const SINK = sinkArg > 0 ? process.argv[sinkArg + 1] : 'http://127.0.0.1:9999';
const DSN = SINK.replace('://', '://publickey@') + '/1';

const exported = [];
const transportFactory = makeOtlpSplitTransport({
  collectorUrl: SINK, // in the browser this is the same-origin '/otlp'
  serviceName: 'demo-frontend',
  environment: 'harness',
  release: 'tail-sample-demo@harness',
  onExport: ({ spans, url, requests, bytes }) => exported.push({ url, count: spans.length, requests, bytes, names: spans.map(s => s.name) }),
});

// ---------------- Part A: real SDK ----------------
Sentry.init({
  dsn: DSN,
  defaultIntegrations: false,
  integrations: [Sentry.spanStreamingIntegration()],
  tracesSampleRate: 1.0,
  enableLogs: true,
  environment: 'harness',
  release: 'tail-sample-demo@harness',
  transport: transportFactory,
});

const partA = {};
await Sentry.startNewTrace(async () => {
  const root = Sentry.startInactiveSpan({ name: 'harness root', op: 'ui.action.demo', forceTransaction: true, attributes: { 'demo.force_keep': 'true', 'demo.count': 3, 'demo.ratio': 0.5, 'demo.flag': true, 'demo.list': ['a', 'b'] } });
  partA.traceId = root.spanContext().traceId;
  partA.rootSpanId = root.spanContext().spanId;
  Sentry.withActiveSpan(root, () => {
    Sentry.startSpan({ name: 'GET /api/v1/models', op: 'http.client' }, () => {});
    Sentry.startSpan({ name: 'render', op: 'ui.render' }, () => {
      partA.errorEventId = Sentry.captureException(new TypeError('harness: error inside span'));
    });
    Sentry.logger.info('harness log line', { scenario: 'harness' });
  });
  root.end();
});
partA.orphanErrorEventId = Sentry.captureException(new Error('harness: error with no active span'));
await new Promise(r => setTimeout(r, 1200)); // streaming buffer flushes 500 ms after segment end
await Sentry.flush(3000);

// ---------------- Part B: hand-made envelopes ----------------
const transport = transportFactory({
  url: `${SINK}/api/1/envelope/?sentry_key=publickey&sentry_version=7`,
  recordDroppedEvent: () => {},
});
const hex = n => [...crypto.getRandomValues(new Uint8Array(n))].map(b => b.toString(16).padStart(2, '0')).join('');
const traceB = hex(16);
const now = Date.now() / 1000;
const v2 = (name, extra = {}) => ({ trace_id: traceB, span_id: hex(8), name, start_timestamp: now - 0.2, end_timestamp: now, status: 'ok', is_segment: false, attributes: { 'sentry.op': { type: 'string', value: 'ui' } }, ...extra });

const partB = { traceId: traceB };
// 1) v1 standalone span (INP-like) + malformed span item + event, in one envelope
partB.mixedResponse = await transport.send([
  { sent_at: new Date().toISOString() },
  [
    [{ type: 'span' }, { trace_id: traceB, span_id: hex(8), description: 'click button', op: 'ui.interaction.click', start_timestamp: now - 0.1, timestamp: now, data: { 'sentry.op': 'ui.interaction.click', inp: 120 } }],
    [{ type: 'span', content_type: 'application/vnd.sentry.items.span.v2+json' }, { version: 2, items: [null, { nonsense: true }, v2('valid-after-garbage')] }],
    [{ type: 'event' }, { event_id: hex(16), timestamp: now, level: 'error', exception: { values: [{ type: 'Error', value: 'hand-made error' }] }, contexts: { trace: { trace_id: traceB, span_id: hex(8) } } }],
  ],
]);
// 2) span-only envelope -> must resolve { statusCode: 200 } without touching Sentry
partB.spanOnlyResponse = await transport.send([{}, [[{ type: 'span', content_type: 'application/vnd.sentry.items.span.v2+json' }, { version: 2, items: [v2('span-only')] }]]]);
// 3) big batch (~200 KiB) -> several OTLP requests, each <= 60 KiB
const big = Array.from({ length: 400 }, (_, i) => v2(`big-${i}`, { attributes: { pad: { type: 'string', value: 'x'.repeat(400) } } }));
await transport.send([{}, [[{ type: 'span', content_type: 'application/vnd.sentry.items.span.v2+json' }, { version: 2, items: big }]]]);
partB.bigBatch = { requests: exported.at(-1).requests, bytes: exported.at(-1).bytes };
await new Promise(r => setTimeout(r, 1000));

console.log(
  JSON.stringify(
    {
      sink: SINK,
      partA,
      partB,
      exports: exported.map(e => ({ url: e.url, count: e.count, requests: e.requests, names: e.names.slice(0, 10) })),
    },
    null,
    2,
  ),
);
