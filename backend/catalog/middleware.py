"""Demo-only middleware. Not needed in a customer setup."""

import sentry_sdk
from opentelemetry import trace as otel_trace

FORCE_KEEP_HEADER = "x-demo-force-keep"


class DemoForceKeepMiddleware:
    """
    If the request carries `x-demo-force-keep: 1`, tag the OTel server span with
    `demo.force_keep="true"`. The collector's test-only `string_attribute` policy
    keeps those traces, which is how the perf-issue scenarios (S8/S9) get 100% of
    their traces into Sentry without lowering the latency threshold for everything.

    The value is a *string* because the tail_sampling `string_attribute` policy only
    matches string attributes.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.headers.get(FORCE_KEEP_HEADER) == "1":
            span = otel_trace.get_current_span()
            if span.is_recording():
                span.set_attribute("demo.force_keep", "true")
            sentry_sdk.set_tag("demo.force_keep", "true")
        return self.get_response(request)
