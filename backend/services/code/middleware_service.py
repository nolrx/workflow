"""
Middleware service — the data/infra layer for a generated full-stack app.

Two responsibilities:

  1. **Generate the data layer** (the ``code_middleware_provisioning`` run):
     from the shared middleware manifest + the flow's ``## 数据设计`` section,
     produce a portable ``init.sql`` (DDL), optional seed data, and an
     entity/index spec. A text-model call does this; it degrades to a
     deterministic extraction when no provider is configured.

  2. **Provision the per-project namespace** (called by the deploy run): create a
     dedicated database inside the SHARED postgres (``app_<pid>``) — or a
     container-local sqlite file when postgres isn't configured (dev) — plus a
     redis key prefix. Returns the connection strings injected into the generated
     backend container, and supports teardown for atomic-deploy rollback.

The generated backend OWNS its migrations where possible (most frameworks
self-migrate on boot); the generated ``init.sql`` is a fallback the deploy step
applies only when no migration mechanism is detected. Comments in English.
"""
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from backend.models.code import CodeProject
from backend.services.prompts import prompt_store

logger = logging.getLogger(__name__)

_MAX_FLOW_CHARS = 12_000


@dataclass
class ProvisionResult:
    """Outcome of provisioning a per-project middleware namespace."""

    applicable: bool
    engine_kind: str  # 'postgres' | 'sqlite' | 'none'
    db_name: Optional[str] = None
    database_url: Optional[str] = None  # injected into the backend container (container-reachable host)
    redis_url: Optional[str] = None
    redis_prefix: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        # NOTE: database_url may carry credentials — callers redact before logging.
        return {
            "applicable": self.applicable,
            "engine_kind": self.engine_kind,
            "db_name": self.db_name,
            "redis_prefix": self.redis_prefix,
            "has_database_url": bool(self.database_url),
            "has_redis_url": bool(self.redis_url),
            "error": self.error,
        }


def _sanitized_db_name(project_id: str) -> str:
    """A valid postgres identifier derived from the project id (``app_<hex>``)."""
    hex_id = re.sub(r"[^a-z0-9]", "", project_id.lower())
    return f"app_{hex_id}"[:48]


# --- 1. Data-layer generation ------------------------------------------------
def _md_section(markdown: str, *keywords: str) -> str:
    current, buf, hit = None, [], []
    for line in (markdown or "").splitlines():
        m = re.match(r"^#{2,4}\s+(.*)$", line.strip())
        if m:
            if current is not None and any(k in current for k in keywords):
                hit.append("\n".join(buf).strip())
            current = m.group(1).strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None and any(k in current for k in keywords):
        hit.append("\n".join(buf).strip())
    return "\n\n".join(b for b in hit if b)


def _extract_json(text_value: str) -> Optional[dict]:
    if not text_value:
        return None
    cleaned = text_value.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL | re.IGNORECASE)
    if fenced:
        cleaned = fenced.group(1).strip()
    if not cleaned.startswith("{"):
        match = re.search(r"(\{.*\})", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(1)
    try:
        value = json.loads(cleaned)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        return None


def _fallback_data_layer(project: CodeProject, manifest: dict) -> dict:
    data_section = _md_section(project.development_flow or "", "数据设计", "数据")
    datastores = manifest.get("datastores") or []
    store_lines = "\n".join(
        f"-- datastore: {d.get('type')} — {d.get('purpose', '')}" for d in datastores
    )
    return {
        "summary": "确定性回退:未配置文本模型,数据层依据「数据设计」原文,由后端自带迁移负责建表。",
        "entities": [],
        "init_sql": (
            "-- 初始化由生成后端自带的迁移负责。以下为数据设计原文(供参考)。\n"
            f"{store_lines}\n-- 数据设计:\n"
            + "\n".join(f"-- {ln}" for ln in (data_section or "").splitlines()[:200])
        ),
        "seed_sql": "",
        "notes": "无 AI 时的回退数据层,实际建表交给后端迁移。",
        "_degraded": True,
    }


def generate_data_layer(project: CodeProject, manifest: dict, contract_summary: str) -> dict:
    """Produce ``{summary, entities, init_sql, seed_sql, notes}`` for the manifest.

    Model-driven with a deterministic fallback. The backend self-migrates where
    possible; ``init_sql`` is the deploy-time fallback.
    """
    from backend.services.ai import get_text_provider

    provider = get_text_provider()
    if not provider or not provider.is_configured():
        return _fallback_data_layer(project, manifest)
    try:
        template = prompt_store.get("code/middleware_prompt.txt")
    except Exception:  # noqa: BLE001
        return _fallback_data_layer(project, manifest)

    prompt = (
        template
        .replace("[[DATA_DESIGN]]", _md_section(project.development_flow or "", "数据设计", "数据")[:_MAX_FLOW_CHARS])
        .replace("[[MANIFEST]]", json.dumps(manifest, ensure_ascii=False)[:6000])
        .replace("[[CONTRACT]]", (contract_summary or "")[:8000])
    )
    try:
        result = provider.generate_text(prompt)
    except Exception as error:  # noqa: BLE001
        logger.warning("data-layer generation raised: %s", error)
        return _fallback_data_layer(project, manifest)
    if not result.success:
        return _fallback_data_layer(project, manifest)
    parsed = _extract_json(result.text)
    if not parsed:
        return _fallback_data_layer(project, manifest)
    parsed.setdefault("summary", "")
    parsed.setdefault("entities", [])
    parsed.setdefault("init_sql", "")
    parsed.setdefault("seed_sql", "")
    return parsed


# --- 2. Namespace provisioning (deploy time) ---------------------------------
def _admin_database_url() -> Optional[str]:
    """The platform's own DATABASE_URL (used to create per-project databases)."""
    return os.getenv("DATABASE_URL") or os.getenv("SQLALCHEMY_DATABASE_URI")


def _project_database_url(admin_url: str, db_name: str) -> str:
    """Build the per-project connection URL, KEEPING the real password.

    ``str(URL)`` renders the password as ``***`` (SQLAlchemy masks it to avoid
    leaking it in logs) — injecting that into the backend container yields a
    literal ``***`` password and a 28P01 auth failure. ``render_as_string(
    hide_password=False)`` emits the real credential.
    """
    return make_url(admin_url).set(database=db_name).render_as_string(hide_password=False)


def provision_namespace(project_id: str) -> ProvisionResult:
    """Create the per-project database + redis prefix in the SHARED infra.

    Postgres: ``CREATE DATABASE app_<pid>`` on the shared server, returning a URL
    that targets it by the compose service host so the backend container can
    reach it. No postgres (dev/sqlite): returns a container-local sqlite path and
    marks redis unavailable. Idempotent (a pre-existing database is reused).
    """
    redis_url = os.getenv("REDIS_URL")
    redis_prefix = f"app:{_sanitized_db_name(project_id)}:"
    admin_url = _admin_database_url()

    if not admin_url or admin_url.startswith("sqlite"):
        # Dev / no shared postgres: the generated backend uses a container-local
        # sqlite file under its writable /app/data dir.
        return ProvisionResult(
            applicable=True, engine_kind="sqlite",
            db_name=None, database_url="sqlite:////app/data/app.db",
            redis_url=redis_url if redis_url and not redis_url.startswith("redis://localhost") else None,
            redis_prefix=redis_prefix if redis_url else None,
        )

    db_name = _sanitized_db_name(project_id)
    try:
        url = make_url(admin_url)
        # Connect to the maintenance DB to issue CREATE DATABASE (no transaction).
        maint = url.set(database="postgres")
        engine = create_engine(maint, isolation_level="AUTOCOMMIT")
        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": db_name}
            ).scalar()
            if not exists:
                # Identifier is derived from a uuid (alnum only) → safe to interpolate.
                conn.execute(text(f'CREATE DATABASE "{db_name}"'))
        engine.dispose()
        # The backend container reaches postgres by the compose service host the
        # platform's own DATABASE_URL already uses (e.g. "postgres"). Render WITH
        # the password (str(URL) would mask it as *** → 28P01 auth failure).
        project_url = _project_database_url(admin_url, db_name)
        return ProvisionResult(
            applicable=True, engine_kind="postgres",
            db_name=db_name, database_url=project_url,
            redis_url=redis_url, redis_prefix=redis_prefix,
        )
    except Exception as error:  # noqa: BLE001 — surfaced to the deploy run for rollback
        logger.error("namespace provisioning failed for %s: %s", project_id, error, exc_info=True)
        return ProvisionResult(applicable=False, engine_kind="postgres", error=str(error))


def apply_init_sql(database_url: str, init_sql: str) -> tuple[bool, str]:
    """Best-effort apply the generated DDL/seed to the provisioned database.

    Returns ``(ok, log)``. Used only as a fallback when the backend has no
    self-migration; statement errors are tolerated (the backend may also create
    tables on boot), so this never sinks a deploy on its own.
    """
    sql = (init_sql or "").strip()
    if not sql or database_url.startswith("sqlite"):
        return True, "no init.sql applied (empty or sqlite-local)"
    applied, errors = 0, []
    try:
        engine = create_engine(database_url, isolation_level="AUTOCOMMIT")
        with engine.connect() as conn:
            for stmt in _split_sql(sql):
                try:
                    conn.execute(text(stmt))
                    applied += 1
                except Exception as se:  # noqa: BLE001 — tolerate per-statement errors
                    errors.append(str(se)[:200])
        engine.dispose()
    except Exception as error:  # noqa: BLE001
        return False, f"connect failed: {error}"
    log = f"applied {applied} statement(s)" + (f"; {len(errors)} error(s): {errors[:3]}" if errors else "")
    return True, log


def _split_sql(sql: str) -> list[str]:
    """Naive ``;`` split that respects dollar-quoted bodies (functions).

    Pure-comment lines are dropped from each statement so a leading ``--`` line
    doesn't swallow the statement that follows it.
    """
    def _clean(lines: list[str]) -> str:
        return (
            "\n".join(ln for ln in lines if not ln.strip().startswith("--"))
            .strip()
            .rstrip(";")
            .strip()
        )

    out, buf, in_dollar = [], [], False
    for line in sql.splitlines():
        if "$$" in line:
            in_dollar = not in_dollar
        buf.append(line)
        if not in_dollar and line.rstrip().endswith(";"):
            stmt = _clean(buf)
            if stmt:
                out.append(stmt)
            buf = []
    tail = _clean(buf)
    if tail:
        out.append(tail)
    return out


def teardown_namespace(db_name: Optional[str]) -> bool:
    """Drop a provisioned per-project database (atomic-deploy rollback)."""
    if not db_name:
        return True
    admin_url = _admin_database_url()
    if not admin_url or admin_url.startswith("sqlite"):
        return True
    try:
        maint = make_url(admin_url).set(database="postgres")
        engine = create_engine(maint, isolation_level="AUTOCOMMIT")
        with engine.connect() as conn:
            # Terminate connections, then drop.
            conn.execute(
                text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"),
                {"n": db_name},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        engine.dispose()
        return True
    except Exception as error:  # noqa: BLE001
        logger.error("namespace teardown failed for %s: %s", db_name, error, exc_info=True)
        return False
