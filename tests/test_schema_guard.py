"""
Platform schema self-heal: ADD missing model columns to existing tables.

Reproduces the production failure where ``agent_steps`` predated the
``port_bindings_raw`` model column (create_all never backfills columns; no
Alembic), so an AgentStep INSERT died with UndefinedColumn. ensure_model_columns()
must add the missing column back so inserts succeed again.
"""
from datetime import datetime

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text

from backend.app import create_app
from backend.extensions import db


@pytest.fixture
def app():
    application = create_app("testing")
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa_inspect(db.engine).get_columns(table)}


def test_ensure_model_columns_backfills_dropped_column(app):
    from backend.services.schema_guard import ensure_model_columns

    # Simulate the drift: the live table lacks a column the model declares.
    try:
        db.session.execute(text("ALTER TABLE agent_steps DROP COLUMN port_bindings_raw"))
        db.session.commit()
    except Exception:  # noqa: BLE001 — sqlite too old for DROP COLUMN
        db.session.rollback()
        pytest.skip("sqlite build does not support DROP COLUMN")

    assert "port_bindings_raw" not in _columns("agent_steps")

    added = ensure_model_columns()
    assert "agent_steps.port_bindings_raw" in added
    assert "port_bindings_raw" in _columns("agent_steps")

    # The INSERT that used to fail now succeeds.
    from backend.models.agent import AgentRun, AgentStep

    run = AgentRun(user_id="u1", domain="code", workflow="code_app_iteration_analysis")
    db.session.add(run)
    db.session.commit()
    step = AgentStep(
        run_id=run.id, agent_key="iteration_analyst", agent_name="影响分析",
        role="planner", order_index=0, status="running", started_at=datetime.utcnow(),
    )
    db.session.add(step)
    db.session.commit()
    assert db.session.get(AgentStep, step.id) is not None


def test_ensure_model_columns_noop_when_in_sync(app):
    """A fresh, in-sync schema needs no changes (idempotent no-op)."""
    from backend.services.schema_guard import ensure_model_columns

    assert ensure_model_columns() == []
