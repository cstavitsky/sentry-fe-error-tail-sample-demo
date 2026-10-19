"""
Keep the backend half of every browser-started trace ("FE direct" mode).

Use this when the browser sends 100% of its spans straight to Sentry (standard Sentry
browser SDK, `tracesSampleRate: 1.0`, no custom transport). Then the frontend half of
every trace is already in Sentry, but the collector's tail sampler never sees browser
spans or browser errors. Without this middleware, a browser-only error would show up in
Sentry with frontend spans only: the Django/Go spans of that trace would be dropped.

What it does: if the request comes from a *sampled* browser trace, put
`sentry.browser_trace="true"` on the OTel server span. The collector's `browser-traces`
policy (string_attribute) then keeps the whole trace, including downstream services.

"From a sampled browser trace" = the request has a `sentry-trace: <trace>-<span>-1`
header whose trace id is the trace OTel is recording (so the browser also sent
`traceparent`, i.e. `propagateTraceparent: true`). The backend's own Sentry SDK does not
add `sentry-trace` to outgoing calls when Sentry tracing is off, so backend-to-backend
calls don't match. Other services that run the Sentry SDK *with* tracing enabled would.

Add it to MIDDLEWARE anywhere: the OTel Django instrumentation inserts its own
middleware first, so the server span is already active here.
"""

from opentelemetry import trace as otel_trace

ATTRIBUTE = "sentry.browser_trace"  # string value: string_attribute only matches strings


class KeepBrowserTracesMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        parts = request.headers.get("sentry-trace", "").strip().split("-")
        if len(parts) == 3 and parts[2] == "1":
            span = otel_trace.get_current_span()
            ctx = span.get_span_context()
            if span.is_recording() and format(ctx.trace_id, "032x") == parts[0].lower():
                span.set_attribute(ATTRIBUTE, "true")
        return self.get_response(request)
