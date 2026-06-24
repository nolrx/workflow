"""
Unit tests for restart-resume reconciliation (``reconcile_orphaned_runs``).

Network-free: ``agent_runtime.start`` is stubbed so no workflow actually runs; we
assert only the reconciliation routing — which orphaned runs get RESUMED
(re-dispatched from their persisted phase) vs FAILED, the crash-loop cap, and the
interrupted-step cleanup. This is the behaviour that lets an in-flight run survive
a backend restart instead of being lost.
"""
from datetime import datetime

import pytest

from backend.app import create_app
from backend.extensions import db
from backend.models.agent import AgentRun, AgentRunStatus, AgentStep, AgentStepStatus
from backend.services.agent import runtime as rt


@pytest.fixture
def app():
    application = create_app("testing")
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture(autouse=True)
def _stub_start(monkeypatch):
    """Record dispatched run ids instead of submitting them to the executor."""
    started: list[str] = []
    monkeypatch.setattr(rt.agent_runtime, "start", lambda app, run_id: started.append(run_id))
    return started


def _mk_run(workflow, status, *, started=True, resume_count=0, credit=0) -> AgentRun:
    run = AgentRun(
        user_id="u1", domain="code", workflow=workflow, status=status,
        credit_reserved=credit,
        started_at=datetime.utcnow() if started else None,
    )
    if resume_count:
        run.set_config({"_resume_count": resume_count})
    db.session.add(run)
    db.session.commit()
    return run


def _reload(run_id: str) -> AgentRun:
    """Re-read a run from the DB (reconcile commits in a nested app context)."""
    db.session.expire_all()
    return db.session.get(AgentRun, run_id)


def test_main_workflow_resumes_via_retry(app, _stub_start):
    """A crashed conversation run continues from its interrupted stage, not failed."""
    run = _mk_run("code_full_generation", AgentRunStatus.RUNNING)
    step = AgentStep(
        run_id=run.id, agent_key="documents", agent_name="Docs",
        order_index=4, status=AgentStepStatus.RUNNING,
    )
    db.session.add(step)
    db.session.commit()
    step_id = step.id

    rt.reconcile_orphaned_runs(app)

    run = _reload(run.id)
    assert run.status == AgentRunStatus.RUNNING  # kept active, not failed
    cfg = run.get_config()
    assert cfg["_resume"]["action"] == "retry"
    assert cfg["_resume"]["reason"] == "service_restart"
    assert cfg["_resume_count"] == 1
    assert run.id in _stub_start  # re-dispatched to the executor
    # The interrupted step is marked failed (honest timeline + deterministic
    # stage-derivation for the retry).
    assert db.session.get(AgentStep, step_id).status == AgentStepStatus.FAILED


def test_from_scratch_workflow_resumes_without_directive(app, _stub_start):
    """A container-build run re-dispatches (re-runs from step 1) — no retry cursor."""
    run = _mk_run("code_frontend_project_generation", AgentRunStatus.RUNNING)

    rt.reconcile_orphaned_runs(app)

    run = _reload(run.id)
    assert run.status == AgentRunStatus.RUNNING
    cfg = run.get_config()
    assert "_resume" not in cfg  # from-scratch workflows ignore a cursor directive
    assert cfg["_resume_count"] == 1
    assert run.id in _stub_start


def test_deploy_is_failed_not_resumed(app, _stub_start):
    """A crashed deploy is NOT auto-resumed (side-effectful); it is failed."""
    run = _mk_run("code_fullstack_deploy", AgentRunStatus.RUNNING)

    rt.reconcile_orphaned_runs(app)

    run = _reload(run.id)
    assert run.status == AgentRunStatus.FAILED
    assert run.completed_at is not None
    assert run.id not in _stub_start  # never re-dispatched


def test_resume_budget_caps_a_crash_loop(app, _stub_start):
    """A run that already exhausted its resume budget is failed, not resumed again."""
    run = _mk_run(
        "code_full_generation", AgentRunStatus.RUNNING,
        resume_count=rt.MAX_RESUME_ATTEMPTS,
    )

    rt.reconcile_orphaned_runs(app)

    run = _reload(run.id)
    assert run.status == AgentRunStatus.FAILED
    assert run.id not in _stub_start


def test_never_started_queued_run_dispatches_fresh(app, _stub_start):
    """A QUEUED row a worker never reached is (re)dispatched fresh — for any workflow."""
    run = _mk_run("code_fullstack_deploy", AgentRunStatus.QUEUED, started=False)

    rt.reconcile_orphaned_runs(app)

    run = _reload(run.id)
    assert run.status == AgentRunStatus.QUEUED  # untouched; worker emits RUN_STARTED
    assert run.started_at is None
    assert run.get_config()["_resume_count"] == 1
    assert run.id in _stub_start


def test_paused_run_is_left_alone(app, _stub_start):
    """PAUSED runs await user input, not a worker — reconcile must not touch them."""
    run = _mk_run("code_full_generation", AgentRunStatus.PAUSED)

    handled = rt.reconcile_orphaned_runs(app)

    run = _reload(run.id)
    assert run.status == AgentRunStatus.PAUSED
    assert run.id not in _stub_start
    assert handled == 0
