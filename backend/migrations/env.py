"""Alembic environment for Worksflow.

Design (see docs/composable-workflow-schema.md §1.2):

``db.create_all()`` remains the schema authority — any fresh database (dev,
tests, a brand-new prod) gets the full current schema, including every column
defined on the models. Migrations exist to patch an EXISTING, already-populated
database incrementally, and each is written GUARDED (add-if-missing, skip if the
table/column is absent or already present). So ``alembic upgrade head`` is a
no-op on a fresh / up-to-date DB and only fills gaps on an older one — the two
paths converge regardless of order.

The DB URL is resolved exactly like the app (``DATABASE_URL`` with the dev SQLite
fallback) so migrations hit the same database the app uses.
"""
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

import backend.models  # noqa: F401 — importing populates db.metadata
from backend.extensions import db

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Mirror DevelopmentConfig.SQLALCHEMY_DATABASE_URI (prod sets DATABASE_URL).
db_url = os.getenv("DATABASE_URL", "sqlite:///ai_creative_studio.db")
config.set_main_option("sqlalchemy.url", db_url)

target_metadata = db.metadata


def run_migrations_offline() -> None:
    """Emit SQL without a DB connection (``alembic upgrade --sql``)."""
    context.configure(
        url=db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
