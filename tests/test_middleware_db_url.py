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

from backend.services.code import deploy_service, middleware_service


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


# --- container_database_url: async-driver scheme adaptation ------------------
# A SQLAlchemy-async backend handed the bare ``postgresql://`` we provision crashes
# on import ("'psycopg2' is not async"). The injected (container-only) URL must carry
# an async driver scheme; the provisioned (platform-side, psycopg2) URL is left sync.
_SYNC_URL = "postgresql://user:s3cret@postgres:5432/app_abc?sslmode=disable"


def test_container_url_asyncpg_rewrites_scheme_drops_sslmode_keeps_password():
    out = middleware_service.container_database_url(_SYNC_URL, "asyncpg")
    url = make_url(out)
    assert url.drivername == "postgresql+asyncpg"
    # libpq's sslmode is not an asyncpg connect kwarg → dropped
    assert "sslmode" not in url.query
    assert url.password == "s3cret"
    assert url.database == "app_abc"


def test_container_url_psycopg_v3_keeps_sslmode():
    url = make_url(middleware_service.container_database_url(_SYNC_URL, "psycopg"))
    assert url.drivername == "postgresql+psycopg"
    assert url.query.get("sslmode") == "disable"


def test_container_url_no_driver_or_sqlite_is_unchanged():
    assert middleware_service.container_database_url(_SYNC_URL, None) == _SYNC_URL
    assert middleware_service.container_database_url(
        "sqlite:////app/data/app.db", "asyncpg"
    ) == "sqlite:////app/data/app.db"


def test_container_url_already_async_is_noop():
    src = "postgresql+asyncpg://u:p@h/db"
    assert middleware_service.container_database_url(src, "asyncpg") == src


def test_detect_async_pg_driver_picks_asyncpg_for_sqla_async():
    source = {
        "app/database.py": b"from sqlalchemy.ext.asyncio import create_async_engine\n"
                           b"engine = create_async_engine(DATABASE_URL)",
        "requirements.txt": b"sqlalchemy\nasyncpg\nfastapi",
    }
    assert deploy_service._detect_async_pg_driver(source) == "asyncpg"


def test_detect_async_pg_driver_none_for_sync_and_direct_asyncpg():
    sync = {"app/db.py": b"from sqlalchemy import create_engine\nengine = create_engine(URL)",
            "requirements.txt": b"sqlalchemy\npsycopg2-binary"}
    go = {"main.go": b'sql.Open("postgres", os.Getenv("DATABASE_URL"))'}
    # direct asyncpg (no SQLAlchemy) wants a RAW postgresql:// DSN → must NOT be adapted
    direct = {"app/db.py": b"import asyncpg\nconn = await asyncpg.connect(DATABASE_URL)",
              "requirements.txt": b"asyncpg"}
    assert deploy_service._detect_async_pg_driver(sync) is None
    assert deploy_service._detect_async_pg_driver(go) is None
    assert deploy_service._detect_async_pg_driver(direct) is None
