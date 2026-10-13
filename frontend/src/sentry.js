// Sentry browser SDK setup. Imported first by main.jsx.
import * as Sentry from '@sentry/react';
import { makeOtlpSplitTransport } from './otlpSplitTransport';

const env = import.meta.env;
const environment = env.VITE_SENTRY_ENVIRONMENT || 'demo';
const release = env.VITE_SENTRY_RELEASE || 'tail-sample-demo@0.1.0';
const otlpUrl = env.VITE_OTLP_URL || '/otlp';
const otlpExportUrl = `${otlpUrl.replace(/\/$/, '')}/v1/traces`;

Sentry.init({
  dsn: env.VITE_SENTRY_DSN_FE,
  environment,
  release,

  integrations: [
    Sentry.browserTracingIntegration(),
    // Opt-in on 10.x: sets traceLifecycle 'stream', so spans are sent as span-v2
    // envelope items (what the OTLP split transport converts). Note: in stream mode
    // beforeSendTransaction / ignoreTransactions are not applied, and a beforeSendSpan
    // callback must be wrapped with Sentry.withStreamedSpan(...).
    Sentry.spanStreamingIntegration(),
    Sentry.replayIntegration(),
  ],

  // REQUIRED by this design, not a demo convenience: the browser must keep 100% so
  // the OTel Collector makes the real (tail-based) decision. The backend uses
  // parentbased_always_on, so a browser-unsampled request (traceparent ...-00) would
  // produce no backend spans at all. Drop noisy browser spans with `ignoreSpans` /
  // `beforeSendSpan: Sentry.withStreamedSpan(...)` instead; both run before the transport.
  tracesSampleRate: 1.0,

  // Send W3C `traceparent` (besides sentry-trace/baggage) so the OTel-instrumented
  // backend continues the browser's trace.
  propagateTraceparent: true,
  tracePropagationTargets: [/^\/api\//, /^\/graphql\//],

  enableLogs: true,
  replaysSessionSampleRate: 1.0, // demo value
  replaysOnErrorSampleRate: 1.0,

  // The transport's own OTLP export requests use the global fetch, so they would show up
  // as `fetch` breadcrumbs on every error and in Replay's network tab. Drop them here
  // (returning null also keeps them out of Replay, which listens to beforeAddBreadcrumb).
  beforeBreadcrumb(breadcrumb) {
    if (breadcrumb.category === 'fetch' && String(breadcrumb.data?.url ?? '').includes(otlpExportUrl)) return null;
    return breadcrumb;
  },

  // Spans -> same-origin /otlp (nginx -> collector). Everything else -> Sentry.
  transport: makeOtlpSplitTransport({
    collectorUrl: otlpUrl,
    serviceName: 'demo-frontend',
    environment,
    release,
  }),
});

export { Sentry };
