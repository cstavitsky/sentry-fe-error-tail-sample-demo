#!/usr/bin/env node
// Independent strict test of frontend/src/otlpSplitTransport.js against the real
// @sentry/react 10.65.0 client (Node, no DOM). No sink needed: global fetch is stubbed
// and every request is recorded. Writes OTLP bodies to $OUT (default /tmp/strict) for
// tests/local/otlp_validate.py, and prints PASS/FAIL per check.
//
//   node tests/local/transport_strict.mjs && python3 tests/local/otlp_validate.py /tmp/strict/otlp_bodies.json
import * as Sentry from '../../frontend/node_modules/@sentry/react/build/esm/index.js';
import { parseEnvelope } from '../../frontend/node_modules/@sentry/core/build/esm/utils/envelope.js';
import { makeOtlpSplitTransport } from '../../frontend/src/otlpSplitTransport.js';
import fs from 'node:fs';
import { isDeepStrictEqual } from 'node:util';

const OUT = process.env.OUT || '/tmp/strict';
fs.mkdirSync(OUT, { recursive: true });
const results = [];
const check = (name, ok, detail = '') => { results.push({ name, ok: !!ok, detail: String(detail) }); };

// ---- fetch stub ------------------------------------------------------------------
const requests = [];
let kaInFlight = 0, kaMax = 0;
globalThis.fetch = async (url, init = {}) => {
  const body = init.body;
  const size = typeof body === 'string' ? Buffer.byteLength(body) : body?.byteLength ?? 0;
  const rec = { url: String(url), keepalive: !!init.keepalive, size, body, headers: init.headers };
  requests.push(rec);
  if (rec.keepalive) { kaInFlight += size; kaMax = Math.max(kaMax, kaInFlight); }
  await new Promise(r => setTimeout(r, 50)); // keep requests "in flight" for a bit
  if (rec.keepalive) kaInFlight -= size;
  return new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } });
};

const inputs = []; // envelopes handed to transport.send (deep-cloned)
const factory = makeOtlpSplitTransport({ collectorUrl: '/otlp', serviceName: 'demo-frontend', environment: 'strict', release: 'r@1' });
const recordingFactory = opts => {
  const t = factory(opts);
  return { send(env) { inputs.push(structuredClone(env)); return t.send(env); }, flush: t.flush };
};

Sentry.init({
  dsn: 'https://publickey@o1.ingest.example.invalid/1',
  defaultIntegrations: false,
  integrations: [Sentry.spanStreamingIntegration()],
  tracesSampleRate: 1.0,
  enableLogs: true,
  transport: recordingFactory,
});

const A = {};
await Sentry.startNewTrace(async () => {
  const root = Sentry.startInactiveSpan({ name: 'root', op: 'ui.action.demo', forceTransaction: true,
    attributes: { 'demo.force_keep': 'true', 'i': 3, 'd': 0.5, 'b': true, 'arr': ['a', 'b'], 'arrn': [1, 2] } });
  A.trace = root.spanContext().traceId; A.root = root.spanContext().spanId;
  Sentry.withActiveSpan(root, () => {
    Sentry.startSpan({ name: 'GET /api/x', op: 'http.client' }, () => {});
    Sentry.startSpan({ name: 'render', op: 'ui.render' }, s => {
      A.renderSpan = s.spanContext().spanId;
      A.ev1 = Sentry.captureException(new TypeError('inside span'));
    });
    A.msgErr = Sentry.captureMessage('message at error level', 'error');
    A.msgInfo = Sentry.captureMessage('info message', 'info');
    Sentry.logger.info('a log');
  });
  root.end();
});
A.orphan = Sentry.captureException(new Error('no active span'));
await new Promise(r => setTimeout(r, 1500));
await Sentry.flush(3000);
await new Promise(r => setTimeout(r, 300));

// ---- decode -----------------------------------------------------------------------
const otlpReqs = () => requests.filter(r => r.url.endsWith('/otlp/v1/traces'));
const sentryReqs = () => requests.filter(r => r.url.includes('/envelope/'));
const allOtlpSpans = () => otlpReqs().flatMap(r => JSON.parse(r.body).resourceSpans.flatMap(rs => rs.scopeSpans.flatMap(ss => ss.spans)));
const decodeSentry = r => parseEnvelope(typeof r.body === 'string' ? r.body : new Uint8Array(r.body));
const asJson = p => (p instanceof Uint8Array ? JSON.parse(new TextDecoder().decode(p)) : p);

// Part A checks
const spansA = allOtlpSpans();
const byName = n => spansA.filter(s => s.name === n);
check('A: root/http/render spans exported', byName('root').length === 1 && byName('GET /api/x').length === 1 && byName('render').length === 1);
check('A: trace id preserved on all regular spans', ['root', 'GET /api/x', 'render'].every(n => byName(n)[0]?.traceId === A.trace));
check('A: root span id preserved', byName('root')[0]?.spanId === A.root);
check('A: root has no parentSpanId key', byName('root')[0] && !('parentSpanId' in byName('root')[0]));
check('A: http.client kind=3 (CLIENT), others 1', byName('GET /api/x')[0]?.kind === 3 && byName('render')[0]?.kind === 1);
check('A: children parent = root', byName('render')[0]?.parentSpanId === A.root && byName('GET /api/x')[0]?.parentSpanId === A.root);
const attrs = s => Object.fromEntries(s.attributes.map(a => [a.key, a.value]));
const ra = attrs(byName('root')[0] || { attributes: [] });
check('A: attr types (string/int-as-string/double/bool/array)', ra['demo.force_keep']?.stringValue === 'true' && ra['i']?.intValue === '3' && ra['d']?.doubleValue === 0.5 && ra['b']?.boolValue === true && ra['arr']?.arrayValue?.values?.[0]?.stringValue === 'a', JSON.stringify({ i: ra.i, d: ra.d, arrn: ra.arrn }));

// markers
const markers = byName('sentry.error_marker');
const eventsSent = sentryReqs().flatMap(r => decodeSentry(r)[1]).filter(([h]) => h.type === 'event').map(([, p]) => asJson(p));
const evById = Object.fromEntries(eventsSent.map(e => [e.event_id, e]));
for (const [label, id] of [['exception in span', A.ev1], ['captureMessage(error)', A.msgErr], ['exception, no active span', A.orphan]]) {
  const ev = evById[id];
  const m = markers.find(s => attrs(s)['sentry.event_id']?.stringValue === id);
  check(`marker: ${label}`, ev && m && m.traceId === ev.contexts.trace.trace_id && m.parentSpanId === ev.contexts.trace.span_id && m.status?.code === 2 && attrs(m)['sentry.project']?.stringValue === 'fe',
    m ? `marker trace=${m.traceId} parent=${m.parentSpanId} | event trace=${ev?.contexts?.trace?.trace_id} span=${ev?.contexts?.trace?.span_id}` : 'no marker');
}
check('marker: exception-in-span parent == render span id', markers.find(s => attrs(s)['sentry.event_id']?.stringValue === A.ev1)?.parentSpanId === A.renderSpan);
check('marker: none for info message', !markers.some(s => attrs(s)['sentry.event_id']?.stringValue === A.msgInfo) && !!evById[A.msgInfo]);
check('marker: exactly 3 markers in part A', markers.length === 3, `got ${markers.length}`);

// non-span items unchanged
let unchanged = true; const detail = [];
const sentAll = sentryReqs().map(decodeSentry);
const norm = env => JSON.parse(JSON.stringify([env[0], env[1].map(([h, p]) => [h, asJson(p)])]));
const sentNorm = sentAll.map(norm);
for (const inp of inputs) {
  const rest = inp[1].filter(([h]) => h.type !== 'span');
  if (!rest.length) continue;
  const want = JSON.parse(JSON.stringify([inp[0], rest]));
  if (!sentNorm.some(s => isDeepStrictEqual(s, want))) { unchanged = false; detail.push(rest.map(([h]) => h.type).join(',')); }
}
check('A: non-span items + envelope headers reach Sentry unchanged (JSON deep-equal)', unchanged, detail.join(' | '));
check('A: no span item ever sent to Sentry', sentAll.every(([, items]) => items.every(([h]) => h.type !== 'span')));
const typesSent = sentAll.flatMap(([, it]) => it.map(([h]) => h.type));
check('A: event + log forwarded', typesSent.includes('event') && typesSent.includes('log'), typesSent.join(','));

// ---- Part B: hand-made edge cases via a fresh transport --------------------------
const t = factory({ url: 'https://o1.ingest.example.invalid/api/1/envelope/?sentry_key=k&sentry_version=7', recordDroppedEvent: () => {} });
const hex = n => [...crypto.getRandomValues(new Uint8Array(n))].map(b => b.toString(16).padStart(2, '0')).join('');
const now = Date.now() / 1000;
const tr = hex(16);
const v2 = (name, extra = {}) => ({ trace_id: tr, span_id: hex(8), name, start_timestamp: now - 0.2, end_timestamp: now, status: 'ok', is_segment: false, attributes: { 'sentry.op': { type: 'string', value: 'ui' } }, ...extra });
const beforeS = sentryReqs().length;
const r1 = await t.send([{}, [[{ type: 'span', content_type: 'application/vnd.sentry.items.span.v2+json' }, { version: 2, items: [v2('only')] }]]]);
check('B: span-only envelope resolves {statusCode:200}, no Sentry request', r1?.statusCode === 200 && sentryReqs().length === beforeS, JSON.stringify(r1));
let threw = false, r2;
try {
  r2 = await t.send([{}, [
    [{ type: 'span' }, null],
    [{ type: 'span' }, { version: 2, items: [null, 42, { nonsense: true }, v2('good')] }],
    [{ type: 'span' }, { version: 2, items: 'not-an-array' }],
    [{ type: 'event' }, { event_id: hex(16), level: 'error', exception: { values: [{ type: 'E', value: 'x' }] } }], // no contexts.trace
    [{ type: 'event' }, null],
  ]]);
} catch (e) { threw = e; }
check('B: malformed items do not throw; result 200', !threw && r2?.statusCode === 200, String(threw || JSON.stringify(r2)));
const last = sentryReqs().at(-1); const lastItems = last ? decodeSentry(last)[1] : [];
check('B: both event items (incl. null payload) forwarded', lastItems.filter(([h]) => h.type === 'event').length === 2, `${lastItems.length} items`);
check('B: valid span after garbage still exported', allOtlpSpans().some(s => s.name === 'good'));
await t.send([{}, [[{ type: 'span' }, { version: 2, items: [{ name: 'no-ids', start_timestamp: now, end_timestamp: now, attributes: {} }, { trace_id: 'XYZ', span_id: '1', name: 'bad-ids', start_timestamp: now, end_timestamp: now, attributes: {} }] }]]]);
const badEmitted = allOtlpSpans().filter(s => s.name === 'no-ids' || s.name === 'bad-ids');
check('B: spans with missing/invalid ids are NOT emitted (else the collector rejects the whole request)', badEmitted.length === 0, `emitted: ${badEmitted.map(s => s.name + ' traceId=' + s.traceId).join('; ')}`);
await t.send([{}, [[{ type: 'span' }, { version: 2, items: [v2('edge-attrs', { attributes: { obj: { type: 'string', value: { a: 1 } }, nan: { type: 'double', value: NaN }, mixed: { type: 'array', value: [1, 'a', true, { o: 1 }] }, u: { type: 'unknown', value: 7 } } })] }]]]);
const edge = allOtlpSpans().find(s => s.name === 'edge-attrs');
const validAny = v => { if (!v || typeof v !== 'object') return false; const k = Object.keys(v); if (k.length !== 1) return false; const x = v[k[0]];
  return (k[0] === 'stringValue' && typeof x === 'string') || (k[0] === 'doubleValue' && typeof x === 'number') || (k[0] === 'intValue' && /^-?\d+$/.test(String(x))) || (k[0] === 'boolValue' && typeof x === 'boolean') || (k[0] === 'arrayValue' && Array.isArray(x.values) && x.values.every(validAny)); };
check('B: edge attribute values (object/NaN/mixed array/unknown type) serialise to valid AnyValue', edge && edge.attributes.every(a => validAny(a.value)), JSON.stringify(edge?.attributes));
const big = Array.from({ length: 600 }, (_, i) => v2(`big-${i}`, { attributes: { pad: { type: 'string', value: 'x'.repeat(400) } } }));
const nb = otlpReqs().length;
await t.send([{}, [[{ type: 'span' }, { version: 2, items: big }]]]);
const bigReqs = otlpReqs().slice(nb);
const bigSpans = bigReqs.flatMap(r => JSON.parse(r.body).resourceSpans[0].scopeSpans[0].spans);
check('chunking: every OTLP body <= 61440 B', bigReqs.every(r => r.size <= 61440), bigReqs.map(r => r.size).join(','));
check('chunking: no span lost/duplicated (600)', bigSpans.length === 600 && new Set(bigSpans.map(s => s.spanId)).size === 600);
check('chunking: keepalive bytes in flight never > 64 KiB', kaMax <= 65536, `max in flight ${kaMax}`);
check('chunking: overflow requests sent without keepalive', bigReqs.filter(r => !r.keepalive).length >= 1, bigReqs.map(r => r.keepalive).join(','));
const nh = otlpReqs().length;
await t.send([{}, [[{ type: 'span' }, { version: 2, items: [v2('huge', { attributes: { pad: { type: 'string', value: 'y'.repeat(70000) } } })] }]]]);
const huge = otlpReqs().slice(nh);
check('chunking: single span > 64 KiB sent once, without keepalive', huge.length === 1 && !huge[0].keepalive, `size=${huge[0]?.size} keepalive=${huge[0]?.keepalive}`);
await new Promise(r => setTimeout(r, 300));

const rs0 = JSON.parse(otlpReqs()[0].body).resourceSpans[0];
const res = Object.fromEntries(rs0.resource.attributes.map(a => [a.key, a.value.stringValue]));
check('resource: service.name / deployment.environment / sdk version', res['service.name'] === 'demo-frontend' && res['deployment.environment'] === 'strict' && res['telemetry.sdk.version'] === '10.65.0', JSON.stringify(res));
check('OTLP request content-type application/json', otlpReqs().every(r => (r.headers?.['content-type'] || '').startsWith('application/json')));

fs.writeFileSync(`${OUT}/otlp_bodies.json`, JSON.stringify(otlpReqs().map(r => JSON.parse(r.body))));
for (const r of results) console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name}${r.detail ? '  :: ' + r.detail.slice(0, 400) : ''}`);
console.log(`otlp requests=${otlpReqs().length} sentry requests=${sentryReqs().length}; bodies -> ${OUT}/otlp_bodies.json`);
process.exit(0);
