#!/usr/bin/env node
// Reproduces the shared keepalive quota problem: browsers fail any keepalive fetch
// whose body would push the document's in-flight keepalive bytes over 64 KiB
// (Fetch spec, "HTTP-network-or-cache fetch", inflightKeepaliveBytes). The OTLP split
// transport and Sentry's makeFetchTransport each keep their OWN 60 KB counter, so
// together they can exceed the shared quota and the Sentry request is the one lost.
//
//   node tests/local/keepalive_quota.mjs
import * as Sentry from '../../frontend/node_modules/@sentry/react/build/esm/index.js';
import { makeOtlpSplitTransport } from '../../frontend/src/otlpSplitTransport.js';

const QUOTA = 64 * 1024;
let inflight = 0;
const log = [];
globalThis.fetch = async (url, init = {}) => {
  const size = typeof init.body === 'string' ? Buffer.byteLength(init.body) : init.body?.byteLength ?? 0;
  const kind = String(url).includes('/otlp/') ? 'otlp' : 'sentry';
  if (init.keepalive) {
    if (inflight + size > QUOTA) {
      log.push({ kind, size, keepalive: true, result: `NETWORK ERROR (inflight ${inflight} + ${size} > ${QUOTA})` });
      throw new TypeError('Failed to fetch');
    }
    inflight += size;
  }
  log.push({ kind, size, keepalive: !!init.keepalive, result: 'sent' });
  await new Promise(r => setTimeout(r, 200)); // slow network: requests stay in flight
  if (init.keepalive) inflight -= size;
  return new Response('{}', { status: 200 });
};

const dropped = [];
const factory = makeOtlpSplitTransport({ collectorUrl: '/otlp' });
const t = factory({ url: 'https://o1.ingest.example.invalid/api/1/envelope/?sentry_key=k&sentry_version=7', recordDroppedEvent: (reason, category) => dropped.push({ reason, category }) });

const hex = n => [...crypto.getRandomValues(new Uint8Array(n))].map(b => b.toString(16).padStart(2, '0')).join('');
const now = Date.now() / 1000;
const tr = hex(16);
// ~55 KB of page-load spans (resource spans etc.) -> one keepalive OTLP chunk
const spans = Array.from({ length: 120 }, (_, i) => ({ trace_id: tr, span_id: hex(8), name: `resource.script /assets/chunk-${i}.js`, start_timestamp: now - 1, end_timestamp: now, status: 'ok', attributes: { 'sentry.op': { type: 'string', value: 'resource.script' }, 'url.full': { type: 'string', value: 'https://example.invalid/assets/' + 'x'.repeat(300) } } }));
const p1 = t.send([{}, [[{ type: 'span' }, { version: 2, items: spans }]]]);
// at the same moment the SDK flushes an error event (~20 KB: breadcrumbs, stack trace)
const p2 = t.send([{ event_id: hex(16) }, [[{ type: 'event' }, { event_id: hex(16), level: 'error', exception: { values: [{ type: 'TypeError', value: 'boom' }] }, breadcrumbs: Array.from({ length: 100 }, (_, i) => ({ message: 'crumb ' + i + ' ' + 'y'.repeat(150) })), contexts: { trace: { trace_id: tr, span_id: hex(8) } } }]]]);
const r2 = await p2.catch(e => ({ error: String(e) }));
await p1;
await new Promise(r => setTimeout(r, 500));
console.log(JSON.stringify({ requests: log, sentrySendResult: r2, recordDroppedEvent: dropped }, null, 1));
const lost = log.some(l => l.kind === 'sentry' && l.result.startsWith('NETWORK ERROR'));
console.log(lost ? 'REPRODUCED: Sentry error request failed because OTLP keepalive bytes were in flight' : 'not reproduced');
