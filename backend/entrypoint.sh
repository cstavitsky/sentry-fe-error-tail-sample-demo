#!/bin/sh
# Starts the Django API in one of two tracing modes (see demo/sentry_setup.py).
set -e

# Demo shortcut: with several replicas, run migrations as a separate one-off job instead.
python manage.py migrate --noinput

GUNICORN="gunicorn demo.wsgi:application --bind 0.0.0.0:8000 --worker-class gthread --workers 2 --threads 32 --timeout 60"

if [ "${BE_TRACING_MODE:-otlp}" = "sentry" ]; then
  echo "BE_TRACING_MODE=sentry: Sentry SDK tracing, OpenTelemetry disabled"
  export OTEL_SDK_DISABLED=true
  exec $GUNICORN
fi

echo "BE_TRACING_MODE=otlp: OpenTelemetry tracing -> ${OTEL_EXPORTER_OTLP_ENDPOINT}, Sentry = errors + logs"
# opentelemetry-instrument (opentelemetry-distro) configures the TracerProvider and OTLP
# exporter from OTEL_* env vars and auto-instruments django, psycopg and requests.
# BatchSpanProcessor re-initialises itself after gunicorn forks its workers.
exec opentelemetry-instrument $GUNICORN
