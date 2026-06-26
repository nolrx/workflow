"""add agent_steps.port_bindings_raw (composable-workflow node bindings)

First migration. Introduces the typed-PortValue binding column on AgentStep so a
composable-workflow (canvas) run records its data lineage for replay (§7).

GUARDED + idempotent (see migrations/env.py): on a fresh DB ``create_all`` already
made the column from the model, so this is a no-op; on an older, already-populated
DB it adds the missing column. Either order is safe.

Revision ID: 0001_port_bindings
Revises:
Create Date: 2026-06-26
"""
import sqlalchemy as sa
from alembic import op

revision = "0001_port_bindings"
down_revision = None
branch_labels = None
depends_on = None

_TABLE = "agent_steps"
_COLUMN = "port_bindings_raw"


def _has_column(inspector) -> bool:
    if _TABLE not in inspector.get_table_names():
        return True  # table absent → create_all owns it; nothing to patch
    return _COLUMN in {c["name"] for c in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not _has_column(inspector):
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE in inspector.get_table_names() and _COLUMN in {
        c["name"] for c in inspector.get_columns(_TABLE)
    }:
        op.drop_column(_TABLE, _COLUMN)
