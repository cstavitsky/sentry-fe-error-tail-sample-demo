# Andrei Borza — sentry-javascript#22118 comment 5892229034 (verbatim)

> We will not have an OTLP exporter in the browser SDKs, but you can use a custom transport (and a couple of helpers to transform sentry spans to OTLP-compatible spans) that sends spans to your collector and everything else to Sentry:

```js
import * as Sentry from '@sentry/browser';

const COLLECTOR_URL = 'https://collector.example.com';

const TYPE_KEYS = { string: 'stringValue', integer: 'intValue', double: 'doubleValue', boolean: 'boolValue' };

function toOtlpValue({ type, value }) {
  if (type === 'array') {
    return { arrayValue: { values: value.map(v => toOtlpValue({ type: typeof v === 'number' ? 'double' : typeof v, value: v })) } };
  }
  return { [TYPE_KEYS[type] ?? 'stringValue']: type === 'integer' ? String(value) : value };
}

const toNano = seconds => String(BigInt(Math.round(seconds * 1e6)) * 1000n);

function toOtlpSpan(span) {
  return {
    traceId: span.trace_id,
    spanId: span.span_id,
    parentSpanId: span.parent_span_id,
    name: span.name,
    kind: span.attributes['sentry.op']?.value === 'http.client' ? 3 : 1,
    startTimeUnixNano: toNano(span.start_timestamp),
    endTimeUnixNano: toNano(span.end_timestamp),
    attributes: Object.entries(span.attributes).map(([key, attr]) => ({ key, value: toOtlpValue(attr) })),
    status: { code: span.status === 'error' ? 2 : 0 },
  };
}

function makeOtlpSplitTransport(options) {
  const sentryTransport = Sentry.makeFetchTransport(options);

  return {
    send([headers, items]) {
      const spans = items.filter(([h]) => h.type === 'span').flatMap(([, c]) => c.items);
      const rest = items.filter(([h]) => h.type !== 'span');

      if (spans.length) {
        // Don't create spans for the export request itself
        Sentry.suppressTracing(() =>
          fetch(`${COLLECTOR_URL}/v1/traces`, {
            method: 'POST',
            keepalive: true,
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({
              resourceSpans: [{
                resource: { attributes: [{ key: 'service.name', value: { stringValue: 'frontend' } }] },
                scopeSpans: [{ spans: spans.map(toOtlpSpan) }],
              }],
            }),
          }),
        ).catch(() => {});
      }

      return rest.length ? sentryTransport.send([headers, rest]) : Promise.resolve({});
    },
    flush: timeout => sentryTransport.flush(timeout),
  };
}

Sentry.init({
  dsn: '__DSN__',
  integrations: [Sentry.browserTracingIntegration()],
  tracesSampleRate: 1.0,
  transport: makeOtlpSplitTransport,
});
```

> Your collector must be reachable from the browser and allow your app's origin via `cors.allowed_origins` on the OTLP HTTP receiver.
> We can think of adding these helpers and the transport to the SDKs but no plans right now.
