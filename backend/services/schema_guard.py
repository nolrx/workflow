"""
Platform schema self-heal — additive column reconcile (no Alembic).

``db.create_all()`` only creates ABSENT tables; a model column added AFTER a table
was first created is never backfilled, so an INSERT referencing it fails with
``UndefinedColumn`` (Postgres) / ``no column named ...`` (sqlite). Since this
project has no migration tool, this best-effort boot pass reflects each existing
table's live columns and ``ALTER TABLE ... ADD COLUMN`` for every column the model
declares but the table lacks.

ADD-only, always NULLable, never drops / alters / narrows — safe on a populated
table and idempotent (``IF NOT EXISTS`` on Postgres; the per-column inspect guard
covers sqlite). Mirrors the deploy-time ``reconcile_schema`` philosophy for
generated apps, applied here to the platform's OWN tables. Fails soft — a single
failed ALTER (lock / concurrent worker) is logged and skipped, never blocks boot.
"""
import logging

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text

from backend.extensions import db

logger = logging.getLogger(__name__)


def ensure_model_columns() -> list[str]:
    """ADD missing model columns to existing tables. Returns the columns added."""
    engine = db.engine
    dialect = engine.dialect
    is_pg = dialect.name == "postgresql"
    inspector = sa_inspect(engine)
    try:
        existing_tables = set(inspector.get_table_names())
    except Exception as error:  # noqa: BLE001 — DB unreachable: nothing to do
        logger.warning("ensure_model_columns: cannot list tables: %s", error)
        return []

    added: list[str] = []
    for table in db.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # create_all() owns wholly-new tables
        try:
            live_cols = {c["name"] for c in inspector.get_columns(table.name)}
        except Exception:  # noqa: BLE001
            continue
        for col in table.columns:
            if col.name in live_cols:
                continue
            try:
                col_type = col.type.compile(dialect=dialect)
            except Exception:  # noqa: BLE001 — exotic type we can't render: skip
                continue
            # Always add NULLable: adding a NOT NULL column with no default to a
            # populated table fails; the app's own column default still fills it
            # on write. We never enforce NOT NULL retroactively.
            guard = "IF NOT EXISTS " if is_pg else ""
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN {guard}"{col.name}" {col_type}'
            try:
                with engine.begin() as conn:
                    conn.execute(text(ddl))
                added.append(f"{table.name}.{col.name}")
            except Exception as error:  # noqa: BLE001 — concurrent worker / lock: skip
                logger.warning("ensure_model_columns: %s skipped: %s", ddl, error)
    if added:
        logger.info("ensure_model_columns added missing columns: %s", added)
    return added
