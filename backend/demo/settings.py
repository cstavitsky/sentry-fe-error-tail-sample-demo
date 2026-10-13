"""Django settings for the tail-sampling demo API (deliberately minimal)."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "demo-only-not-a-secret")
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "catalog",
]

MIDDLEWARE = [
    "django.middleware.common.CommonMiddleware",
    # Demo-only: copies the `x-demo-force-keep` request header onto the server span
    # so the collector's (test-only) string_attribute policy can keep the trace.
    "catalog.middleware.DemoForceKeepMiddleware",
]

ROOT_URLCONF = "demo.urls"
WSGI_APPLICATION = "demo.wsgi.application"

# Postgres in Docker; sqlite fallback for the local (no Docker) harness.
if os.environ.get("POSTGRES_HOST"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "HOST": os.environ["POSTGRES_HOST"],
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
            "NAME": os.environ.get("POSTGRES_DB", "demo"),
            "USER": os.environ.get("POSTGRES_USER", "demo"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "demo"),
            "CONN_MAX_AGE": 60,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.environ.get("SQLITE_PATH", str(BASE_DIR / "db.sqlite3")),
            "OPTIONS": {"timeout": 30},
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
APPEND_SLASH = False

# Downstream "replica" service (Go) and the slow third-party billing provider.
GO_REPLICA_URL = os.environ.get("GO_REPLICA_URL", "http://go-replica:8090").rstrip("/")
BILLING_PROVIDER_URL = os.environ.get("BILLING_PROVIDER_URL", f"{GO_REPLICA_URL}/billing/quote")
BILLING_DELAY_MS = int(os.environ.get("BILLING_DELAY_MS", "1800"))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}

# ---------------------------------------------------------------------------
# Sentry: errors + logs (and, in BE_TRACING_MODE=sentry, tracing). See sentry_setup.py
# ---------------------------------------------------------------------------
from demo.sentry_setup import init_sentry  # noqa: E402

init_sentry()
