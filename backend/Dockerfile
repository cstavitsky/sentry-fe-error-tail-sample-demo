FROM python:3.12-slim

# Customer runs 2.56.0; override to test another release, e.g.
#   docker compose build --build-arg SENTRY_PYTHON_VERSION=2.71.0 backend
ARG SENTRY_PYTHON_VERSION=2.56.0

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
# One resolver run, so the sentry-sdk extra can't silently move the pinned OTel packages.
RUN pip install --no-cache-dir -r requirements.txt "sentry-sdk[opentelemetry-otlp]==${SENTRY_PYTHON_VERSION}"

COPY . .
RUN chmod +x entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["./entrypoint.sh"]
