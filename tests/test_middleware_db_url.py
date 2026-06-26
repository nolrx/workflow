"""
Unit tests for the per-project DATABASE_URL the deploy injects into the backend
container (``middleware_service._project_database_url``).

DB-free, pure-function surface. The critical invariant: the injected URL pins an
explicit ``sslmode`` so a generated backend whose driver DEFAULTS to
``sslmode=require`` (Go ``lib/pq``/``pgx``, …) does not crash-loop with
``pq: SSL is not enabled on the server`` against the SSL-less shared Postgres —
while the platform's own psycopg2 (default ``prefer``) had silently worked,
masking the mismatch. The real password must survive (a masked ``***`` → 28P01).
"""
from sqlalchemy.engine import make_url

from backend.services.code import middleware_service


def test_injected_url_pins_sslmode_disable_and_keeps_password():
    out = middleware_service._project_database_url(
        "postgresql://user:s3cret@postgres:5432/platform", "app_abc"
    )
    url = make_url(out)
    assert url.database == "app_abc"
    assert url.query.get("sslmode") == "disable"
    # password must NOT be masked to *** (that would 28P01 in the container)
    assert url.password == "s3cret"
    assert url.host == "postgres" and url.port == 5432


def test_explicit_sslmode_in_admin_url_is_respected():
    out = middleware_service._project_database_url(
        "postgresql://u:p@db:5432/plat?sslmode=require", "app_x"
    )
    assert make_url(out).query.get("sslmode") == "require"


def test_existing_query_params_and_driver_suffix_preserved():
    out = middleware_service._project_database_url(
        "postgresql+psycopg2://u:p@db/plat?connect_timeout=10", "app_y"
    )
    url = make_url(out)
    assert url.drivername == "postgresql+psycopg2"
    assert url.query.get("connect_timeout") == "10"
    assert url.query.get("sslmode") == "disable"


def test_sslmode_override_via_env(monkeypatch):
    monkeypatch.setenv("APP_DB_SSLMODE", "prefer")
    out = middleware_service._project_database_url(
        "postgresql://u:p@db:5432/plat", "app_z"
    )
    assert make_url(out).query.get("sslmode") == "prefer"
