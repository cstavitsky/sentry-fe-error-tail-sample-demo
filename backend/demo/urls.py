from django.urls import path

from catalog import views

urlpatterns = [
    path("api/healthz", views.healthz),
    # S1 / S5 / S6: happy path
    path("api/v1/models", views.list_models),
    # S1 (happy) and S4 (?replica_fail=1): Django -> Go replica service
    path("api/v1/models/<int:model_id>/deployments", views.model_deployments),
    # S3: handled exception, still returns 200
    path("api/v1/models/<int:model_id>/config", views.model_config),
    # S8: N+1 DB query pattern
    path("api/v1/models/<int:model_id>/deployment-history", views.deployment_history),
    # S9: slow DB query
    path("api/v1/deployments/status-report", views.status_report),
    # S7: slow third-party call
    path("api/settings/billing", views.billing_settings),
    # S2: unhandled exception in a GraphQL resolver
    path("graphql/", views.graphql),
]
