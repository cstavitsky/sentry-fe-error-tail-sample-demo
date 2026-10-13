"""
Demo API views. Each view backs one or more scenarios (see README "Scenarios").

Nothing here is Sentry- or OTel-specific beyond ordinary `logging` and one
`sentry_sdk.capture_exception` for the handled-error scenario: spans come from
OTel auto-instrumentation (Django, psycopg, requests).
"""

import json
import logging
import time

import requests
import sentry_sdk
from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .models import DeploymentStatus, Model, ModelVersion

logger = logging.getLogger("catalog")

# One shared HTTP session (connection pooling). OTel's requests instrumentation
# creates a CLIENT span per call and injects the W3C `traceparent` header.
http = requests.Session()


class ReplicaUnavailable(Exception):
    """The Go replica service returned an error."""


class ResolverError(Exception):
    """A GraphQL resolver blew up."""


def _model_json(m):
    return {"id": m.id, "slug": m.slug, "name": m.name, "framework": m.framework}


def _replica_status(model_id, fail=False):
    resp = http.get(
        f"{settings.GO_REPLICA_URL}/replica/status",
        params={"model_id": model_id, **({"fail": "1"} if fail else {})},
        timeout=5,
    )
    if resp.status_code >= 500:
        raise ReplicaUnavailable(f"replica service returned {resp.status_code} for model {model_id}")
    resp.raise_for_status()
    return resp.json()


@require_GET
def healthz(request):
    return JsonResponse({"ok": True})


@require_GET
def list_models(request):
    """S1/S5/S6: fast, boring, 200."""
    return JsonResponse({"models": [_model_json(m) for m in Model.objects.order_by("id")]})


@require_GET
def model_deployments(request, model_id):
    """
    S1: Django -> Go replica service -> 200.
    S4: `?replica_fail=1` makes the Go service fail; Django reports it and returns 502.
    """
    model = Model.objects.filter(id=model_id).first()
    if model is None:
        return JsonResponse({"error": "not found"}, status=404)
    versions = list(model.versions.order_by("-version").values("id", "version"))
    try:
        replica = _replica_status(model_id, fail=request.GET.get("replica_fail") == "1")
    except ReplicaUnavailable as exc:
        logger.warning("Replica service failed for model %s", model.slug)
        sentry_sdk.capture_exception(exc)  # before_send marks the OTel span as ERROR
        return JsonResponse({"error": "replica service unavailable"}, status=502)
    return JsonResponse({"model": _model_json(model), "versions": versions, "replica": replica})


@require_GET
def model_config(request, model_id):
    """S3: a *handled* exception. The user still gets a 200."""
    version = ModelVersion.objects.filter(model_id=model_id).order_by("-version").first()
    if version is None:
        return JsonResponse({"error": "not found"}, status=404)
    try:
        config = json.loads(version.config)
        status = "ok"
    except json.JSONDecodeError as exc:
        # Reported to Sentry, but the request succeeds. Without the before_send hook
        # the OTel span would stay UNSET and the tail sampler would drop this trace.
        sentry_sdk.capture_exception(exc)
        logger.warning("Model %s v%s has an invalid config; serving defaults", model_id, version.version)
        config = {}
        status = "degraded"
    return JsonResponse({"model_id": model_id, "version": version.version, "config": config, "status": status})


# --- S8: N+1 --------------------------------------------------------------------------
# One "source" query for the status rows, then one *identical* query per row for its
# version. pg_sleep(0.01) adds ~10 ms per repeat so the repeats total >= 50 ms (the
# N+1 detector's duration threshold); local Postgres would otherwise answer in < 1 ms.
# The SQL text is constant (params use %s), so every repeat has the same description.
N_PLUS_ONE_ROWS = 12
N_PLUS_ONE_SQL = (
    'SELECT "catalog_modelversion".*, pg_sleep(0.01) AS simulated_latency '
    'FROM "catalog_modelversion" WHERE "catalog_modelversion"."id" = %s'
)


@require_GET
def deployment_history(request, model_id):
    # Source query (distinct SQL).
    statuses = list(
        DeploymentStatus.objects.filter(model_version__model_id=model_id).order_by("-id")[:N_PLUS_ONE_ROWS]
    )
    history = []
    for status in statuses:
        # The N+1: one query per row instead of select_related(). Keep this loop free of
        # any other instrumented call (HTTP, cache, custom spans), it would break the run.
        version = list(ModelVersion.objects.raw(N_PLUS_ONE_SQL, [status.model_version_id]))[0]
        history.append({"state": status.state, "replicas": status.replicas, "version": version.version})
    return JsonResponse({"model_id": model_id, "history": history})


# --- S9: Slow DB query ------------------------------------------------------------------
# A single SELECT that takes >= 1000 ms (the Slow DB Query detector's default threshold).
# Constant SQL text, so every request shares one fingerprint (the issue needs 100 hits).
SLOW_QUERY_SQL = "SELECT pg_sleep(1.2)"


@require_GET
def status_report(request):
    with connection.cursor() as cursor:
        cursor.execute(SLOW_QUERY_SQL)
    counts = {s: DeploymentStatus.objects.filter(state=s).count() for s in ("ACTIVE", "FAILED")}
    return JsonResponse({"report": counts})


# --- S7: slow third-party call -----------------------------------------------------------
@require_GET
def billing_settings(request):
    logger.info("Fetching billing quote from provider")
    started = time.monotonic()
    resp = http.get(settings.BILLING_PROVIDER_URL, params={"delay_ms": settings.BILLING_DELAY_MS}, timeout=10)
    resp.raise_for_status()
    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info("Billing quote received in %d ms", elapsed_ms)
    return JsonResponse({"plan": "team", "quote": resp.json(), "provider_ms": elapsed_ms})


# --- S2: GraphQL (simple JSON handler, not a real GraphQL server) ------------------------
@csrf_exempt
@require_POST
def graphql(request):
    try:
        body = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"errors": [{"message": "invalid JSON"}]}, status=400)

    op = body.get("operationName") or "Anonymous"
    variables = body.get("variables") or {}
    logger.info("GraphQL operation %s", op)

    if op == "ListModels":
        return JsonResponse({"data": {"models": [_model_json(m) for m in Model.objects.order_by("id")]}})

    if op == "ModelReplicaDetails":
        model_id = int(variables.get("modelId", 1))
        # Hits the Go service first, so the trace spans FE -> Django -> Go ...
        replica = _replica_status(model_id)
        logger.warning("Resolving replicaDetails for model %s with %d replicas", model_id, len(replica.get("replicas", [])))
        # ... and then the resolver fails: unhandled -> HTTP 500 -> Sentry issue.
        raise ResolverError(f"replicaDetails resolver failed for model {model_id}: unexpected null 'region'")

    return JsonResponse({"errors": [{"message": f"unknown operation {op}"}]}, status=400)
