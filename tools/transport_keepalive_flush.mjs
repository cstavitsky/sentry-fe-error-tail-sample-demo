#!/usr/bin/env node
// Checks the keepalive and flush behaviour of frontend/src/otlpSplitTransport.js
// (no browser, no sink): a fetch stub enforces the browser's shared 64 KiB keepalive
// quota, and `document.visibilityState` is simulated.
//
//   node tools/transport_keepalive_flush.mjs        (exit code 1 on any FAIL)
import * as Sentry from '../frontend/node_modules/@sentry/react/build/esm/index.js';
import { makeOtlpSplitTransport, __test__ } from '../frontend/src/otlpSplitTransport.js';

const QUOTA = 64 * 1024;
let inflight = 0;
let delayMs = 200;
const log = [];
globalThis.fetch = async (url, init = {}) => {
  const size = typeof init.body === 'string' ? Buffer.byteLength(init.body) : init.body?.byteLength ?? 0;
  const kind = String(url).includes('/otlp/') ? 'otlp' : 'sentry';
  const rec = { kind, size, keepalive: !!init.keepalive, result: 'sent', done: false };
  log.push(rec);
  if (init.keepalive) {
    if (inflight + size > QUOTA) {
      rec.result = 'NETWORK ERROR';
      throw new TypeError('Failed to fetch');
    }
    inflight += size;
  }
  await new Promise(r => setTimeout(r, delayMs));
  if (init.keepalive) inflight -= size;
  rec.done = true;
  return new Response('{}', { status: 200 });
};

const results = [];
const check = (name, ok, detail = '') => results.push({ name, ok: !!ok, detail: String(detail) });
const hex = n => [...crypto.getRandomValues(new Uint8Array(n))].map(b => b.toString(16).padStart(2, '0')).join('');
const now = Date.now() / 1000;
const pageSpans = (tr, n) => Array.from({ length: n }, (_, i) => ({ trace_id: tr, span_id: hex(8), name: `resource.script /assets/chunk-${i}.js`, start_timestamp: now - 1, end_timestamp: now, status: 'ok', attributes: { 'sentry.op': { type: 'string', value: 'resource.script' }, 'url.full': { type: 'string', value: 'https://example.invalid/assets/' + 'x'.repeat(300) } } }));
const errorEnvelope = (tr, crumbs) => [{ event_id: hex(16) }, [[{ type: 'event' }, { event_id: hex(16), level: 'error', exception: { values: [{ type: 'TypeError', value: 'boom' }] }, breadcrumbs: Array.from({ length: crumbs }, (_, i) => ({ message: 'crumb ' + i + ' ' + 'y'.repeat(150) })), contexts: { trace: { trace_id: tr, span_id: hex(8) } } }]]];

const dropped = [];
const exportsSeen = [];
const factory = makeOtlpSplitTransport({ collectorUrl: '/otlp', onExport: e => exportsSeen.push(e) });
const t = factory({ url: 'https://o1.ingest.example.invalid/api/1/envelope/?sentry_key=k&sentry_version=7', recordDroppedEvent: (reason, category) => dropped.push({ reason, category }) });

// 1) Page hidden: big OTLP batch first, then two Sentry envelopes (~18 KB and ~55 KB) at the same moment.
globalThis.document = { visibilityState: 'hidden' };
const tr = hex(16);
const p1 = t.send([{}, [[{ type: 'span' }, { version: 2, items: pageSpans(tr, 120) }]]]);
const r2 = t.send(errorEnvelope(tr, 100));
const r3 = t.send(errorEnvelope(tr, 330));
const [s2, s3] = await Promise.all([r2.catch(e => ({ error: String(e) })), r3.catch(e => ({ error: String(e) })), p1]);
await t.flush(5000);
const otlpHidden = log.filter(l => l.kind === 'otlp');
const sentryReqs = log.filter(l => l.kind === 'sentry');
check('hidden: no Sentry request failed (shared keepalive quota respected)', sentryReqs.length === 2 && sentryReqs.every(l => l.result === 'sent') && dropped.length === 0, JSON.stringify({ sentry: sentryReqs.map(l => [l.size, l.keepalive, l.result]), dropped, s2, s3 }));
check('hidden: Sentry requests still used keepalive', sentryReqs.some(l => l.keepalive), sentryReqs.map(l => l.keepalive).join(','));
check(`hidden: every OTLP chunk <= ${__test__.OTLP_KEEPALIVE_BUDGET_BYTES} B (keepalive budget)`, otlpHidden.every(l => l.size <= __test__.OTLP_KEEPALIVE_BUDGET_BYTES), otlpHidden.map(l => l.size).join(','));
check('hidden: at least one OTLP chunk used keepalive', otlpHidden.some(l => l.keepalive));
check('hidden: OTLP keepalive chunks never failed', otlpHidden.every(l => l.result === 'sent'));
const reported = exportsSeen[0]?.bytes ?? []; // [0] = the span batch; later exports are error markers
check('size accounting matches real body bytes', reported.length > 1 && reported.every((b, i) => b === otlpHidden[i].size), `${reported.slice(0, 5)} vs ${otlpHidden.slice(0, 5).map(l => l.size)}`);
const spansOut = exportsSeen[0]?.spans.length;
check('hidden: no span lost in chunking (120)', spansOut === 120, spansOut);

// 2) Page visible: plain fetch for OTLP, 60 KiB chunks.
log.length = 0;
globalThis.document = { visibilityState: 'visible' };
await t.send([{}, [[{ type: 'span' }, { version: 2, items: pageSpans(hex(16), 300) }]]]);
await t.flush(5000);
const otlpVisible = log.filter(l => l.kind === 'otlp');
check('visible: OTLP never uses keepalive', otlpVisible.length > 0 && otlpVisible.every(l => !l.keepalive), otlpVisible.map(l => l.keepalive).join(','));
check('visible: chunks <= 60 KiB', otlpVisible.every(l => l.size <= 60 * 1024), otlpVisible.map(l => l.size).join(','));

// 3) flush() waits for in-flight OTLP exports; times out with false.
log.length = 0;
delayMs = 400;
await t.send([{}, [[{ type: 'span' }, { version: 2, items: pageSpans(hex(16), 3) }]]]);
const ok = await t.flush(2000);
const otlpDone = log.filter(l => l.kind === 'otlp');
check('flush(): resolves only after OTLP posts completed', ok === true && otlpDone.length > 0 && otlpDone.every(l => l.done) && __test__.pendingExports.size === 0, JSON.stringify({ ok, done: otlpDone.map(l => l.done), pending: __test__.pendingExports.size }));
delayMs = 1500;
await t.send([{}, [[{ type: 'span' }, { version: 2, items: pageSpans(hex(16), 3) }]]]);
const t0 = Date.now();
const timedOut = await t.flush(200);
check('flush(timeout): returns false when OTLP export exceeds the timeout', timedOut === false && Date.now() - t0 < 1000, `${timedOut} after ${Date.now() - t0} ms`);
await new Promise(r => setTimeout(r, 1700));

// 4) Breadcrumb filter as configured in frontend/src/sentry.js (same predicate).
const otlpExportUrl = '/otlp/v1/traces';
const beforeBreadcrumb = b => (b.category === 'fetch' && String(b.data?.url ?? '').includes(otlpExportUrl) ? null : b);
check('beforeBreadcrumb drops OTLP export fetches only', beforeBreadcrumb({ category: 'fetch', data: { url: '/otlp/v1/traces', method: 'POST' } }) === null && beforeBreadcrumb({ category: 'fetch', data: { url: '/api/v1/models' } }) !== null);

let fail = 0;
for (const r of results) {
  if (!r.ok) fail++;
  console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name}${r.detail ? '  :: ' + r.detail.slice(0, 300) : ''}`);
}
console.log(fail ? `RESULT FAIL (${fail})` : 'RESULT PASS');
process.exit(fail ? 1 : 0);
