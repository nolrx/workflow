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
        engine = create_engine(
            database_url, isolation_level="AUTOCOMMIT", connect_args=_PG_DDL_CONNECT_ARGS
        )
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


def reset_namespace(database_url: Optional[str]) -> tuple[bool, str]:
    """Reset a provisioned per-project database back to an EMPTY schema.

    Deploy-time recovery: when the pre-applied ``init.sql`` fallback creates a
    schema that conflicts with a *self-migrating* backend's own create-table-on-
    boot (mismatched id types / missing columns → the backend crashes during
    startup and never binds its port), drop everything and let the backend build
    its own schema on the next start. Targets the project's OWN database
    (``app_<pid>``), never the platform db. Sqlite-local / no-db: no-op.
    """
    if not database_url or database_url.startswith("sqlite"):
        return True, "no reset (sqlite-local or none)"
    try:
        engine = create_engine(
            database_url, isolation_level="AUTOCOMMIT", connect_args=_PG_DDL_CONNECT_ARGS
        )
        with engine.connect() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        engine.dispose()
        return True, "database reset to empty schema"
    except Exception as error:  # noqa: BLE001 — surfaced to the deploy run
        logger.error("namespace reset failed: %s", error, exc_info=True)
        return False, f"reset failed: {error}"


_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
# Explicit type ALLOW-LIST for the ADD COLUMN path. ctype comes from an LLM, so a
# permissive "DDL-fragment" regex (letters/digits/()/,/space) would let a type
# string smuggle a constraint clause into ADD COLUMN — `TEXT REFERENCES secret(id)`,
# `TEXT DEFAULT now()`, `TEXT CHECK(...)`, `TEXT GENERATED ...`, `serial`, `TEXT
# COLLATE ...` all pass a fragment regex and emit non-intended DDL. This matches
# ONLY a bare column type (optionally with a size/precision), so anything carrying
# DEFAULT/CHECK/REFERENCES/GENERATED/COLLATE/NOT NULL/`;` is rejected → that column
# is skipped (the only default is the white-listed _SAFE_DEFAULTS, set separately).
_ALLOWED_TYPE_RE = re.compile(
    r"(?i)\A(?:"
    r"text|citext|uuid|boolean|bool|smallint|integer|int|bigint|serial|bigserial|"
    r"real|double\s+precision|"
    r"numeric(?:\s*\(\s*\d+\s*(?:,\s*\d+\s*)?\))?|"
    r"decimal(?:\s*\(\s*\d+\s*(?:,\s*\d+\s*)?\))?|"
    r"varchar(?:\s*\(\s*\d+\s*\))?|character\s+varying(?:\s*\(\s*\d+\s*\))?|"
    r"char(?:\s*\(\s*\d+\s*\))?|"
    r"timestamptz|timestamp(?:\s+with\s+time\s+zone)?|date|time|"
    r"jsonb|json|bytea"
    r")\Z"
)
_SAFE_DEFAULTS = {"now()", "current_timestamp", "false", "true", "0"}
_MAX_RECONCILE_STMTS = int(os.getenv("APP_SCHEMA_RECONCILE_MAX", "500"))
_MAX_RECONCILE_TABLES = int(os.getenv("APP_SCHEMA_RECONCILE_TABLES", "200"))
# Bound DDL on the project DB so a lock we can't acquire fails FAST instead of
# hanging the deploy worker forever: ALTER COLUMN TYPE / DROP SCHEMA need ACCESS
# EXCLUSIVE, and an idle-in-transaction holder would otherwise block indefinitely
# (the blocking conn.execute can't poll cooperative cancellation). A timeout is
# raised as OperationalError and absorbed by the existing per-statement/connect
# try/except → never sinks a /health-green deploy. Postgres-only (these engines
# are only ever built for non-sqlite project URLs).
_PG_DDL_CONNECT_ARGS = {"options": "-c lock_timeout=15s -c statement_timeout=60s"}


def _widen_target(live_dtype: str, live_len: Optional[int], ctype: str) -> Optional[str]:
    """The WIDEN target SQL type for an EXISTING column, or ``None`` for no-op.

    WIDEN-ONLY and minimal: only ever touches a BOUNDED ``character varying``
    column (``live_len is not None``) that is strictly narrower than the contract
    target. An already-unbounded varchar, a ``text`` column, or any non-text column
    is left untouched — this never narrows, never retypes across families, and
    never rewrites a column that is already wide enough.
    """
    if live_dtype != "character varying" or live_len is None:
        return None
    kind, n = _contract_text_target(ctype)
    if kind == "text":
        return "TEXT"  # bounded varchar → unbounded text (kills 22001)
    if kind == "varchar" and n and live_len < n:
        return f"VARCHAR({n})"  # widen to the larger bound only
    return None


def _contract_text_target(type_str: str) -> tuple[Optional[str], Optional[int]]:
    """Classify a contract column SQL type for the WIDEN decision.

    Returns ``("text", None)`` for an unbounded text target, ``("varchar", n)``
    for a bounded varchar, or ``(None, None)`` for a non-textual type (no widen).
    """
    t = (type_str or "").strip().upper()
    m = re.match(r"(?:VARCHAR|CHARACTER VARYING)\s*\(\s*(\d+)\s*\)\Z", t)
    if m:
        return ("varchar", int(m.group(1)))
    if t in ("TEXT", "CITEXT", "VARCHAR", "CHARACTER VARYING"):
        return ("text", None)
    return (None, None)


def reconcile_schema(database_url: Optional[str], db_schema: dict) -> tuple[bool, str]:
    """Align the LIVE per-project DB to the contract's authoritative ``db_schema``.

    The backend ORM is the sole schema author (deploy no longer pre-applies
    init.sql), but the ORM's column WIDTHS are still LLM-chosen — a narrow
    ``VARCHAR(n)`` on a business string column 22001s on the first long write, and
    a contract column the ORM forgot 500s on read. Deterministically, by reading
    ``information_schema`` (language-agnostic ground truth), this:

      * **ADDs** any contract column missing from an existing table (nullable, so
        it is safe on a populated table; a ``now()``-style default is honored), and
      * **WIDENs** a too-narrow textual column to the contract type (``TEXT`` /
        a larger ``VARCHAR(n)``).

    ADD/WIDEN only — it NEVER narrows, drops, or retypes across families, and only
    touches tables the backend already created. Postgres-only (sqlite enforces no
    varchar length and has limited ALTER); sqlite/none → no-op. Best-effort:
    per-statement errors are tolerated and surfaced in the log, so it can never
    sink a ``/health``-green deploy.
    """
    if not database_url or database_url.startswith("sqlite"):
        return True, "no reconcile (sqlite-local or none)"
    tables = (db_schema or {}).get("tables") if isinstance(db_schema, dict) else None
    if not isinstance(tables, list) or not tables:
        return True, "no reconcile (empty db_schema)"

    added, widened, skipped, errors = 0, 0, 0, []
    stmts = 0
    try:
        engine = create_engine(
            database_url, isolation_level="AUTOCOMMIT", connect_args=_PG_DDL_CONNECT_ARGS
        )
        with engine.connect() as conn:
            live_tables = {
                r[0]
                for r in conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
                    )
                )
            }
            for tbl in tables[:_MAX_RECONCILE_TABLES]:
                if stmts >= _MAX_RECONCILE_STMTS:
                    break
                if not isinstance(tbl, dict):
                    continue
                tname = str(tbl.get("name") or "")
                if not _IDENT_RE.match(tname):
                    skipped += 1
                    continue
                if tname not in live_tables:
                    # The backend owns table creation; a missing whole table is a
                    # generation gap, not ours to CREATE (that would re-introduce
                    # the dual-source drift this reconcile exists to prevent).
                    skipped += 1
                    continue
                live_cols = {
                    r[0]: (r[1], r[2])  # name -> (data_type, character_maximum_length)
                    for r in conn.execute(
                        text(
                            "SELECT column_name, data_type, character_maximum_length "
                            "FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND table_name = :t"
                        ),
                        {"t": tname},
                    )
                }
                for col in (tbl.get("columns") or []):
                    if stmts >= _MAX_RECONCILE_STMTS:
                        break
                    if not isinstance(col, dict):
                        continue
                    cname = str(col.get("name") or "")
                    ctype = str(col.get("type") or "").strip()
                    # Identifier + EXPLICIT type allow-list (reject any DDL fragment
                    # carrying DEFAULT/CHECK/REFERENCES/GENERATED/COLLATE/NOT NULL).
                    if not _IDENT_RE.match(cname) or not _ALLOWED_TYPE_RE.match(ctype):
                        skipped += 1
                        continue
                    if cname not in live_cols:
                        # ADD missing column — nullable (safe on a populated table).
                        # Add WITHOUT a default first (catalog-only, no table rewrite),
                        # then SET DEFAULT separately for a recognized literal default
                        # (a volatile DEFAULT now() inline would rewrite the whole table
                        # under ACCESS EXCLUSIVE).
                        default = str(col.get("default") or "").strip().lower()
                        try:
                            conn.execute(
                                text(f'ALTER TABLE "{tname}" ADD COLUMN IF NOT EXISTS "{cname}" {ctype}')
                            )
                            added += 1
                            stmts += 1
                            if default in _SAFE_DEFAULTS and stmts < _MAX_RECONCILE_STMTS:
                                conn.execute(
                                    text(f'ALTER TABLE "{tname}" ALTER COLUMN "{cname}" SET DEFAULT {default}')
                                )
                                stmts += 1
                        except Exception as se:  # noqa: BLE001 — tolerate per-statement
                            errors.append(f"add {tname}.{cname}: {str(se)[:120]}")
                            stmts += 1
                        continue
                    # Column exists → WIDEN it only if it is a too-narrow bounded
                    # varchar (decision is in the pure, unit-tested _widen_target).
                    live_dtype, live_len = live_cols[cname]
                    target = _widen_target(live_dtype, live_len, ctype)
                    if target:
                        try:
                            conn.execute(
                                text(f'ALTER TABLE "{tname}" ALTER COLUMN "{cname}" TYPE {target}')
                            )
                            widened += 1
                        except Exception as se:  # noqa: BLE001 — tolerate per-statement
                            errors.append(f"widen {tname}.{cname}: {str(se)[:120]}")
                        stmts += 1
        engine.dispose()
    except Exception as error:  # noqa: BLE001 — connect failure surfaced, never raised
        return False, f"connect failed: {error}"
    log = f"added {added} column(s), widened {widened}, skipped {skipped}"
    if errors:
        log += f"; {len(errors)} error(s): {errors[:3]}"
    return True, log


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
