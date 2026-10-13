import time

from django.apps import AppConfig
from django.db.backends.signals import connection_created


def _register_sqlite_pg_sleep(sender, connection, **kwargs):
    """
    Local (sqlite) harness only: provide pg_sleep() so the N+1 and slow-query SQL
    is byte-for-byte the same as on Postgres, and the DB spans really take time.
    """
    if connection.vendor == "sqlite":
        connection.connection.create_function("pg_sleep", 1, lambda seconds: time.sleep(float(seconds)))


class CatalogConfig(AppConfig):
    name = "catalog"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        connection_created.connect(_register_sqlite_pg_sleep)
