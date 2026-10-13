"""
Sentry setup for the Django API.

Two modes, selected with the BE_TRACING_MODE env var:

  otlp   (default)  Sentry SDK = errors + logs only.
                    Spans come from the OpenTelemetry SDK (opentelemetry-instrument)
                    and go to the OTel Collector, which tail-samples them into Sentry.
                    Sentry errors/logs are stamped with the *OTel* trace_id/span_id
                    by OTLPIntegration, so they land on the same trace.

  sentry (control)  Plain Sentry SDK tracing (traces_sample_rate=1.0), no OTel export.
                    Only used to compare performance-issue detection (AC6).
"""

import logging
import os

import sentry_sdk
from sentry_sdk.integrations.django import DjangoIntegration
from sentry_sdk.integrations.logging import LoggingIntegration

BE_TRACING_MODE = os.environ.get("BE_TRACING_MODE", "otlp").strip().lower()

# FIX_LOG_SPAN_ID=1 (default) enables the before_send_log workaround below.
# Set FIX_LOG_SPAN_ID=0 to observe the unpatched SDK behaviour.
FIX_LOG_SPAN_ID = os.environ.get("FIX_LOG_SPAN_ID", "1").strip() not in ("0", "false", "no", "")


def _demo_team_for(event):
    """Example of a team-ownership tag, the same idea as a per-team routing tag."""
    url = (event.get("request") or {}).get("url") or ""
    transaction = event.get("transaction") or ""
    if "billing" in url or "billing" in transaction:
        return "billing"
    return "inference"


def _error_description(event, hint):
    """Short "Type: message" description for the OTel span status."""
    exc_info = (hint or {}).get("exc_info")
    if exc_info and exc_info[1] is not None:
        exc = exc_info[1]
        return f"{type(exc).__name__}: {exc}"
    logentry = event.get("logentry") or {}
    return str(logentry.get("formatted") or logentry.get("message") or event.get("message") or "error event")[:500]


def before_send(event, hint):
    event.setdefault("tags", {})["demo_team"] = _demo_team_for(event)

    if BE_TRACING_MODE == "otlp":
        # Handled errors (sentry_sdk.capture_exception, capture_message(level="error"),
        # logger.error(...) via LoggingIntegration) don't change the OTel span, so the
        # collector's tail sampler would never know this trace had an error. Mark the
        # current OTel span ERROR so `status_code: [ERROR]` keeps the trace.
        # (Unhandled exceptions are already marked by the OTel Django instrumentation;
        # doing it again is harmless.)
        # Note: the description below is copied into the span, i.e. also into your
        # existing trace store, outside Sentry's server-side PII scrubbing.
        exc_info = hint.get("exc_info") if hint else None
        has_exception = bool(exc_info and exc_info[1] is not None)
        if has_exception or event.get("level") in ("error", "fatal"):
            from opentelemetry import trace as otel_trace
            from opentelemetry.trace import Status, StatusCode

            span = otel_trace.get_current_span()
            if span.is_recording():
                span.set_status(Status(StatusCode.ERROR, _error_description(event, hint)))
                span.set_attribute("sentry.event_id", event.get("event_id", ""))
    return event


def _otel_ids_for_logs(log, hint):
    """
    Workaround for sentry-sdk >= 2.59.0: inside a Django request the log span_id is
    taken from Sentry's (unsampled, never sent) per-request transaction instead of
    the active OTel span. trace_id is still correct. Re-stamp both from OTel.
    Harmless on 2.56.0, where the ids are already correct.
    """
    from opentelemetry import trace as otel_trace

    ctx = otel_trace.get_current_span().get_span_context()
    if ctx.is_valid:
        log["trace_id"] = format(ctx.trace_id, "032x")
        log["span_id"] = format(ctx.span_id, "016x")
    return log


def _ignore_gunicorn_logs():
    """Keep gunicorn's own log lines ("Booting worker", access logs, ...) out of Sentry
    Logs. They are emitted outside any request, so they would carry random trace ids.
    Error events from gunicorn.error are still captured."""
    try:
        from sentry_sdk.integrations.logging import ignore_logger_for_sentry_logs
    except ImportError:  # older sentry-sdk: one ignore list for events and logs
        from sentry_sdk.integrations.logging import ignore_logger as ignore_logger_for_sentry_logs
    ignore_logger_for_sentry_logs("gunicorn.*")


def init_sentry():
    _ignore_gunicorn_logs()
    common = dict(
        dsn=os.environ.get("SENTRY_DSN_BE") or None,
        environment=os.environ.get("SENTRY_ENVIRONMENT", "demo"),
        release=os.environ.get("SENTRY_RELEASE", "tail-sample-demo@0.1.0"),
        send_default_pii=True,
        enable_logs=True,
        before_send=before_send,
        debug=os.environ.get("SENTRY_DEBUG") == "1",
    )

    if BE_TRACING_MODE == "sentry":
        # ---- Control mode: classic Sentry SDK tracing, no OpenTelemetry --------------
        sentry_sdk.init(
            **common,
            traces_sample_rate=1.0,
            integrations=[
                DjangoIntegration(),
                LoggingIntegration(sentry_logs_level=logging.INFO),
            ],
        )
        return

    # ---- Default mode: OTel owns tracing, Sentry owns errors + logs -----------------
    # pip install "sentry-sdk[opentelemetry-otlp]"
    from sentry_sdk.integrations.otlp import OTLPIntegration

    sentry_sdk.init(
        **common,
        # DO NOT set traces_sample_rate / traces_sampler / enable_tracing, not even 0.0:
        # any value turns on Sentry tracing, and Sentry's own per-request transaction
        # ids would then win over the OTel ids on errors and logs.
        integrations=[
            DjangoIntegration(),
            OTLPIntegration(
                setup_otlp_traces_exporter=False,  # keep ONLY our exporter -> our collector
                setup_propagator=False,  # keep OTel's W3C traceparent propagation
                capture_exceptions=False,  # DjangoIntegration already captures exceptions
            ),
            LoggingIntegration(sentry_logs_level=logging.INFO),  # stdlib logging -> Sentry Logs
        ],
        before_send_log=_otel_ids_for_logs if FIX_LOG_SPAN_ID else None,
    )
