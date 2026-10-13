/**
 * OTLP split transport for @sentry/react / @sentry/browser.
 *
 *   span items  ──► converted to OTLP/HTTP JSON ──► POST {collectorUrl}/v1/traces
 *   everything else (errors, replays, logs, client reports, ...) ──► Sentry, unchanged
 *
 * Based on Andrei Borza's snippet in getsentry/sentry-javascript#22118 (comment
 * 5892229034, copied in research/andrei_snippet.md). Functional changes to that
 * snippet are marked with a `DEVIATION n` comment and explained in TRANSPORT_NOTES.md.
 * Code without a marker keeps Andrei's logic; formatting and naming may differ.
 *
 * Requires span streaming (`Sentry.spanStreamingIntegration()`), so spans arrive as
 * `span` envelope items with content type `application/vnd.sentry.items.span.v2+json`
 * and payload `{ version: 2, items: SerializedStreamedSpan[] }` — verified against
 * @sentry/core 10.65.0 (build/esm/tracing/spans/envelope.js).
 */
// DEVIATION 11: imported from @sentry/react (the SDK this app uses); Andrei's
// example imports @sentry/browser. Both export the same functions used here.
import * as Sentry from '@sentry/react';

// ─── Andrei's helpers (hardened, DEVIATION 10) ─────────────────────────────────────

// DEVIATION 10: values are sanitised so that every attribute is a valid OTLP AnyValue.
// One invalid value makes the collector reject the *whole* request (every span in the
// chunk, incl. other traces' spans and error markers), so it is worth being strict.
function safeString(value) {
  if (typeof value === 'string') return value;
  try {
    const s = JSON.stringify(value);
    return s === undefined ? String(value) : s;
  } catch {
    return String(value); // circular structures, BigInt, ...
  }
}

function primitiveToOtlp(value) {
  if (typeof value === 'boolean') return { boolValue: value };
  if (typeof value === 'number') return Number.isFinite(value) ? { doubleValue: value } : { stringValue: String(value) };
  return { stringValue: safeString(value) }; // strings, objects, null, nested arrays
}

/** Returns an OTLP AnyValue, or null when the attribute should be dropped. */
function toOtlpValue(attr) {
  const { type, value } = attr ?? {};
  if (value === undefined || value === null) return null;
  switch (type) {
    case 'array':
      // Andrei: numbers -> double, everything else by typeof. Non-primitive elements
      // (objects, null, nested arrays) are stringified instead of emitted as invalid values.
      return Array.isArray(value) ? { arrayValue: { values: value.map(primitiveToOtlp) } } : { stringValue: safeString(value) };
    case 'integer':
      if (typeof value === 'number' && Number.isSafeInteger(value)) return { intValue: String(value) }; // int64 as string
      if (typeof value === 'bigint' || (typeof value === 'string' && /^-?\d{1,18}$/.test(value))) return { intValue: String(value) };
      return primitiveToOtlp(value);
    case 'double':
      return typeof value === 'number' ? primitiveToOtlp(value) : { stringValue: safeString(value) };
    case 'boolean':
      return typeof value === 'boolean' ? { boolValue: value } : { stringValue: safeString(value) };
    default: // 'string' and unknown types
      return { stringValue: safeString(value) };
  }
}

const toNano = seconds => String(BigInt(Math.round(seconds * 1e6)) * 1000n);

const HEX32 = /^[0-9a-f]{32}$/;
const HEX16 = /^[0-9a-f]{16}$/;
const ZERO32 = '0'.repeat(32);
const ZERO16 = '0'.repeat(16);
const normId = id => (typeof id === 'string' ? id.toLowerCase() : '');

/**
 * Returns an OTLP span, or null if the span can't be represented (DEVIATION 10:
 * missing/invalid trace or span id, non-numeric timestamps).
 *
 * @param {object} span            span-v2 shaped span (see normalizeSpan)
 * @param {object} [opts]
 * @param {boolean} [opts.httpClientErrorsAsUnset=false]  DEVIATION 12: map http.client
 *        spans that failed with a 4xx response to UNSET instead of ERROR.
 */
function toOtlpSpan(span, opts = {}) {
  const traceId = normId(span?.trace_id);
  const spanId = normId(span?.span_id);
  if (!HEX32.test(traceId) || traceId === ZERO32 || !HEX16.test(spanId) || spanId === ZERO16) return null;
  const parent = normId(span.parent_span_id);
  const start = Number(span.start_timestamp);
  const end = Number(span.end_timestamp ?? span.start_timestamp);
  if (!Number.isFinite(start) || !Number.isFinite(end)) return null;
  const attrs = span.attributes && typeof span.attributes === 'object' ? span.attributes : {};

  const op = attrs['sentry.op']?.value;
  let statusCode = span.status === 'error' ? 2 : 0; // DEVIATION 7 (kept): non-error -> 0 (UNSET)
  if (statusCode === 2 && opts.httpClientErrorsAsUnset && op === 'http.client') {
    const http = Number(attrs['http.response.status_code']?.value);
    if (http >= 400 && http < 500) statusCode = 0;
  }

  const attributes = [];
  for (const [key, attr] of Object.entries(attrs)) {
    const value = toOtlpValue(attr);
    if (value) attributes.push({ key, value });
  }

  return {
    traceId,
    spanId,
    // Empty/invalid parent -> key omitted (root span). undefined is dropped by JSON.stringify.
    parentSpanId: HEX16.test(parent) && parent !== ZERO16 ? parent : undefined,
    name: String(span.name ?? '') || 'span',
    kind: op === 'http.client' ? 3 : 1, // CLIENT : INTERNAL
    startTimeUnixNano: toNano(start),
    endTimeUnixNano: toNano(end),
    attributes,
    status: { code: statusCode },
  };
}

// ─── Additions ─────────────────────────────────────────────────────────────────────

/**
 * DEVIATION 3b: a `span` item can also be a *v1 standalone span* (a single SpanJSON:
 * `description` / `data` / `timestamp`), e.g. INP web-vital spans, which @sentry/core
 * 10.65.0 still sends via createSpanEnvelope() even in streaming mode. Normalise it to
 * the v2 shape so toOtlpSpan() works on it.
 */
function normalizeSpan(span) {
  if (span && span.attributes && span.end_timestamp !== undefined) return span; // already v2
  const attributes = {};
  for (const [key, value] of Object.entries(span?.data ?? {})) {
    if (value === undefined || value === null) continue;
    const type = Array.isArray(value)
      ? 'array'
      : typeof value === 'number'
        ? Number.isInteger(value) ? 'integer' : 'double'
        : typeof value === 'boolean' ? 'boolean' : 'string';
    attributes[key] = { type, value };
  }
  if (span?.op && !attributes['sentry.op']) attributes['sentry.op'] = { type: 'string', value: span.op };
  return {
    trace_id: span.trace_id,
    span_id: span.span_id,
    parent_span_id: span.parent_span_id,
    name: span.description ?? span.op ?? 'span',
    start_timestamp: span.start_timestamp,
    end_timestamp: span.timestamp ?? span.start_timestamp,
    // Same as the SDK's own v1 -> v2 mapping (core spanJsonToStreamedSpan): only
    // statuses other than ok/cancelled become 'error'.
    status: !span.status || span.status === 'ok' || span.status === 'cancelled' ? 'ok' : 'error',
    attributes,
  };
}

const randomHex = bytes =>
  Array.from(crypto.getRandomValues(new Uint8Array(bytes)), b => b.toString(16).padStart(2, '0')).join('');

/**
 * DEVIATION 4: error marker span. Browser errors are Sentry *events*, not spans, so the
 * collector's tail sampler can't see them. For every error event we also send one tiny
 * OTLP span with status ERROR on the event's trace. The collector keeps the trace and
 * then drops the marker (filter processor) before exporting to Sentry.
 *
 * Error events always carry contexts.trace (active span, or the scope's propagation
 * context), so this also works after the page-load span ended — but only while the
 * collector has not decided the trace yet, i.e. within `decision_wait` of the trace's
 * FIRST span reaching the collector. Later errors can't pull the trace in.
 *
 * Note: the exception message is copied into the marker (and so into the trace store),
 * outside Sentry's server-side PII scrubbing. Drop `exception.message` if that matters.
 */
function errorMarkerSpan(event) {
  const trace = event?.contexts?.trace;
  if (!trace?.trace_id) return null;
  const isError = event.exception || event.level === 'error' || event.level === 'fatal';
  if (!isError) return null;
  const ex = event.exception?.values?.[event.exception.values.length - 1];
  const ts = typeof event.timestamp === 'number' ? event.timestamp : Date.now() / 1000;
  const attributes = {
    'sentry.event_id': { type: 'string', value: String(event.event_id ?? '') },
    'sentry.project': { type: 'string', value: 'fe' },
    ...(ex?.type && { 'exception.type': { type: 'string', value: String(ex.type) } }),
    ...(ex?.value && { 'exception.message': { type: 'string', value: String(ex.value).slice(0, 500) } }),
  };
  return {
    trace_id: trace.trace_id,
    span_id: randomHex(8),
    parent_span_id: trace.span_id,
    name: 'sentry.error_marker',
    start_timestamp: ts,
    end_timestamp: ts,
    status: 'error',
    attributes,
  };
}

// DEVIATION 2: keepalive budget.
// Browsers cap the in-flight body size of ALL keepalive requests of a document at
// 64 KiB together (Fetch spec, inflightKeepaliveBytes); a request that would cross the
// cap fails with a network error. Sentry's own makeFetchTransport sets keepalive while
// ITS pending bytes stay <= 60 000 (browser/.../transports/fetch.js), so the only share
// we can take without ever making a Sentry request fail is 64 KiB - 60 000 B.
// We therefore:
//   - use keepalive ONLY when the page is hidden (pagehide / visibilitychange, which is
//     when BrowserClient flushes and when keepalive actually matters), and
//   - then keep the total OTLP keepalive bytes in flight <= 5 536 B and chunk to that
//     size; anything else is sent without keepalive (may be cancelled on unload).
// FE spans are best-effort on unload; Sentry delivery always wins.
const KEEPALIVE_QUOTA_BYTES = 64 * 1024;
const SENTRY_KEEPALIVE_CAP_BYTES = 60000;
const OTLP_KEEPALIVE_BUDGET_BYTES = KEEPALIVE_QUOTA_BYTES - SENTRY_KEEPALIVE_CAP_BYTES; // 5 536
const CHUNK_BYTES = 60 * 1024; // visible page: plain fetch, chunk only to bound request size
let keepaliveBytesInFlight = 0;
const encoder = new TextEncoder();

const pageIsHidden = () => typeof document !== 'undefined' && document.visibilityState === 'hidden';

/** Splits spans into OTLP request bodies of at most `maxBytes` (a single bigger span gets its own body). */
function buildBodies(otlpSpans, resource, scope, maxBytes = CHUNK_BYTES) {
  const wrap = spans => JSON.stringify({ resourceSpans: [{ resource, scopeSpans: [{ scope, spans }] }] });
  const overhead = encoder.encode(wrap([])).length;
  const bodies = [];
  let chunk = [];
  let chunkBytes = overhead;
  const flush = () => {
    const body = wrap(chunk);
    bodies.push({ body, size: chunkBytes - (chunk.length ? 1 : 0) });
    chunk = [];
    chunkBytes = overhead;
  };
  for (const span of otlpSpans) {
    const spanBytes = encoder.encode(JSON.stringify(span)).length + 1; // +1 for the comma
    if (chunk.length && chunkBytes + spanBytes - 1 > maxBytes) flush();
    chunk.push(span);
    chunkBytes += spanBytes;
  }
  if (chunk.length) flush();
  return bodies;
}

// DEVIATION 13: in-flight OTLP requests are tracked so flush() can wait for them.
const pendingExports = new Set();

function postOtlp(url, { body, size }, allowKeepalive) {
  const keepalive = allowKeepalive && keepaliveBytesInFlight + size <= OTLP_KEEPALIVE_BUDGET_BYTES;
  if (keepalive) keepaliveBytesInFlight += size;
  // suppressTracing is REQUIRED in stream mode, not cosmetic: without it the export
  // fetch creates a new root `http.client` span (core fetch instrumentation), which is
  // streamed, exported again, creates another span, ... an endless export loop.
  let request;
  try {
    request = Sentry.suppressTracing(() =>
      fetch(url, {
        method: 'POST',
        keepalive,
        headers: { 'content-type': 'application/json' },
        body,
      }),
    );
  } catch (e) {
    request = Promise.reject(e);
  }
  const p = Promise.resolve(request)
    .catch(() => {}) // never let an OTLP failure affect Sentry delivery
    .finally(() => {
      if (keepalive) keepaliveBytesInFlight -= size;
      pendingExports.delete(p);
    });
  pendingExports.add(p);
  return p;
}

/** Resolves true when all in-flight OTLP exports settled, false on timeout (0/undefined = no timeout). */
function waitForExports(timeout) {
  const all = Promise.allSettled([...pendingExports]).then(() => true);
  if (!timeout) return all;
  return Promise.race([all, new Promise(resolve => setTimeout(() => resolve(false), timeout))]);
}

/**
 * Create the transport factory to pass as `transport` to Sentry.init().
 *
 * DEVIATION 11: curried factory. Andrei passes the transport function itself
 * (`transport: makeOtlpSplitTransport`); here you call it with config first:
 * `transport: makeOtlpSplitTransport({ collectorUrl: '/otlp', ... })`.
 *
 * @param {object}  config
 * @param {string}  [config.collectorUrl='/otlp']     DEVIATION 1: same-origin proxy path
 * @param {string}  [config.serviceName='demo-frontend'] DEVIATION 6: configurable resource
 * @param {string}  [config.environment]
 * @param {string}  [config.release]
 * @param {Array}   [config.extraResourceAttributes]  OTLP KeyValue[] appended to the resource
 * @param {boolean} [config.httpClientErrorsAsUnset=false] DEVIATION 12: 4xx http.client spans -> UNSET
 * @param {(info: object) => void} [config.onExport]   test/debug hook, called per OTLP batch
 */
export function makeOtlpSplitTransport(config = {}) {
  const {
    collectorUrl = '/otlp',
    serviceName = 'demo-frontend',
    environment,
    release,
    extraResourceAttributes = [],
    httpClientErrorsAsUnset = false,
    onExport,
  } = config;
  const url = `${collectorUrl.replace(/\/$/, '')}/v1/traces`;

  // DEVIATION 6: richer resource (Andrei hard-codes service.name='frontend').
  const str = (key, value) => ({ key, value: { stringValue: String(value) } });
  const resource = {
    attributes: [
      str('service.name', serviceName),
      ...(environment ? [str('deployment.environment', environment), str('deployment.environment.name', environment)] : []),
      ...(release ? [str('service.version', release)] : []),
      str('telemetry.sdk.name', 'sentry.javascript.react'),
      str('telemetry.sdk.language', 'webjs'),
      str('telemetry.sdk.version', Sentry.SDK_VERSION),
      ...extraResourceAttributes,
    ],
  };
  const scope = { name: 'sentry.javascript.react', version: Sentry.SDK_VERSION };
  const spanOpts = { httpClientErrorsAsUnset };

  return function makeTransport(options) {
    const sentryTransport = Sentry.makeFetchTransport(options);

    return {
      send([headers, items]) {
        // DEVIATION 3: everything OTLP-related (conversion, chunking, posting) runs inside
        // try/catch, and each span is converted in its own try/catch, so a bad span is
        // skipped and nothing can throw out of send(). (Span envelopes are span-only, so
        // the realistic loss without this is the whole span batch, plus an exception
        // thrown inside the SDK's flush timer.)
        try {
          const spans = [];
          for (const [h, c] of items) {
            if (h?.type !== 'span') continue;
            // `c.items ?? [c]`: v1 standalone spans have no `items` (see DEVIATION 3b).
            const list = Array.isArray(c?.items) ? c.items : c?.items === undefined && c ? [c] : [];
            for (const s of list) {
              try {
                const otlp = toOtlpSpan(normalizeSpan(s), spanOpts);
                if (otlp) spans.push(otlp);
              } catch {
                /* skip malformed span */
              }
            }
          }
          // DEVIATION 4: one ERROR marker span per error event (the event itself still
          // goes to Sentry). Also for envelopes that contain no span items at all.
          for (const [h, c] of items) {
            if (h?.type !== 'event') continue;
            try {
              const marker = errorMarkerSpan(c);
              const otlp = marker && toOtlpSpan(marker, spanOpts);
              if (otlp) spans.push(otlp);
            } catch {
              /* skip */
            }
          }

          if (spans.length) {
            const hidden = pageIsHidden();
            const bodies = buildBodies(spans, resource, scope, hidden ? OTLP_KEEPALIVE_BUDGET_BYTES : CHUNK_BYTES);
            for (const b of bodies) postOtlp(url, b, hidden);
            if (onExport) {
              try {
                onExport({ spans, url, requests: bodies.length, bytes: bodies.map(b => b.size), keepaliveAllowed: hidden });
              } catch {
                /* ignore */
              }
            }
          }
        } catch {
          /* never break Sentry delivery because of OTLP export */
        }

        const rest = items.filter(([h]) => h?.type !== 'span');
        // DEVIATION 5: span-only envelopes resolve { statusCode: 200 } instead of {}, so SDK
        // hooks that look at the transport response see success.
        return rest.length ? sentryTransport.send([headers, rest]) : Promise.resolve({ statusCode: 200 });
      },
      // DEVIATION 13: wait for Sentry's buffer AND in-flight OTLP exports (same timeout).
      flush: timeout =>
        Promise.all([sentryTransport.flush(timeout), waitForExports(timeout)]).then(([sentryOk, otlpOk]) => sentryOk && otlpOk),
    };
  };
}

// Exported for the local test harnesses only.
export const __test__ = {
  toOtlpValue,
  toNano,
  toOtlpSpan,
  normalizeSpan,
  errorMarkerSpan,
  buildBodies,
  pendingExports,
  OTLP_KEEPALIVE_BUDGET_BYTES,
  getKeepaliveBytesInFlight: () => keepaliveBytesInFlight,
};
