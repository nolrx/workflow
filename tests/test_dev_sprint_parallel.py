"""
Unit tests for Dev Mode sprint P3 — the PARALLEL batch scheduler (network/docker-free).

Covers the two pieces of new logic (the git worktree isolation + integration barrier
already ship with ``code_dev_parallel_turn`` and are exercised elsewhere):
  1. conflict-aware batch claim (``claim_ready_batch``): parent_feature_id de-dup,
     asset-runs-solo, k bounded by CODE_DEV_SPRINT_BATCH / lane cap / ready count;
  2. per-task AC fold (``apply_batch_verify_outcomes``): each task judged on its OWN
     acceptance criteria with sibling AC masked out of its regression view;
  3. the env-gated scheduling loop end-to-end (batch child faked via a monkeypatched
     ``agent_runtime.run_sync``): happy path, infra-fail re-queue, and — critically —
     that with the flag OFF the loop still uses the single-task ``claim_next_task`` path.
"""
import uuid

import pytest

from backend.app import create_app
from backend.extensions import db
from backend.models.agent import AgentRun, AgentRunStatus
from backend.models.code import CodeProject
from backend.models.code.fullstack import (
    CodeDevSession,
    CodeDevSprint,
    CodeDevTask,
    DevSessionStatus,
    DevSprintStatus,
    DevTaskStatus,
)
from backend.services.code import dev_sprint_service as svc


@pytest.fixture
def app(tmp_path):
    application = create_app("testing")
    application.config["UPLOAD_FOLDER"] = str(tmp_path / "uploads")
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


def _project(user_id: str) -> CodeProject:
    project = CodeProject(user_id=user_id, title="Sprint P3", requirement_input="做一个后台系统")
    db.session.add(project)
    db.session.commit()
    return project


def _session(project: CodeProject, lane="frontend") -> CodeDevSession:
    s = CodeDevSession(
        project_id=project.id, user_id=project.user_id, lane=lane,
        status=DevSessionStatus.RUNNING,
    )
    db.session.add(s)
    db.session.commit()
    return s


def _task(session, *, fid=None, title="任务", status=DevTaskStatus.PENDING, category="functional",
          lane=None, deps=None, criteria=None, priority=None, max_retries=None, order=0,
          parent=None, retry_count=None) -> CodeDevTask:
    t = CodeDevTask(
        project_id=session.project_id, session_id=session.id, feature_id=fid,
        category=category, lane=lane, title=title, status=status, parent_feature_id=parent,
        priority=priority, max_retries=max_retries, order_index=order, retry_count=retry_count,
    )
    if deps is not None:
        t.set_depends_on(deps)
    if criteria is not None:
        t.set_acceptance_criteria(criteria)
    db.session.add(t)
    db.session.commit()
    return t


# --- config knobs ---------------------------------------------------------------
def test_parallel_flag_default_off(app, monkeypatch):
    monkeypatch.delenv("CODE_DEV_SPRINT_PARALLEL", raising=False)
    assert svc.sprint_parallel_enabled() is False
    for on in ("1", "true", "YES", "on"):
        monkeypatch.setenv("CODE_DEV_SPRINT_PARALLEL", on)
        assert svc.sprint_parallel_enabled() is True


def test_batch_size_bounded_by_lane_cap(app, monkeypatch):
    monkeypatch.setenv("CODE_DEV_SPRINT_BATCH", "8")
    monkeypatch.setenv("DEV_MODE_MAX_PARALLEL", "3")
    assert svc.sprint_batch_size() == 3  # never claim more than the turn's lane cap
    monkeypatch.setenv("DEV_MODE_MAX_PARALLEL", "16")
    assert svc.sprint_batch_size() == 8
    monkeypatch.setenv("CODE_DEV_SPRINT_BATCH", "0")  # floored to 1
    assert svc.sprint_batch_size() == 1


# --- claim_ready_batch ----------------------------------------------------------
def test_batch_dedups_by_parent_feature(app):
    session = _session(_project(str(uuid.uuid4())))
    # Two sub-tasks of the SAME FR must never be claimed into one batch.
    _task(session, fid="FR1.T1", parent="FR1", criteria=["a"], order=1)
    _task(session, fid="FR1.T2", parent="FR1", criteria=["b"], order=2)
    _task(session, fid="FR2.T1", parent="FR2", criteria=["c"], order=3)
    batch = svc.claim_ready_batch(session.id, "frontend", 4)
    fids = {t.feature_id for t in batch}
    assert fids == {"FR1.T1", "FR2.T1"}  # one per parent
    assert all(t.status == DevTaskStatus.QUEUED for t in batch)
    # The skipped sibling stays pending, claimable next round.
    left = [t for t in svc.session_tasks(session.id) if t.status == DevTaskStatus.PENDING]
    assert {t.feature_id for t in left} == {"FR1.T2"}


def test_batch_asset_runs_solo(app):
    session = _session(_project(str(uuid.uuid4())))
    # Asset sorts first (asset-先行); it must be returned ALONE even with room to spare.
    _task(session, fid="A1", parent="A1", category="asset", criteria=["img"], order=1)
    _task(session, fid="FR1.T1", parent="FR1", criteria=["x"], order=2)
    batch = svc.claim_ready_batch(session.id, "frontend", 4)
    assert [t.feature_id for t in batch] == ["A1"]
    # A non-asset task encountered first never pulls an asset into its batch.
    batch2 = svc.claim_ready_batch(session.id, "frontend", 4)
    assert [t.feature_id for t in batch2] == ["FR1.T1"]


def test_batch_bounded_by_k(app):
    session = _session(_project(str(uuid.uuid4())))
    for i in range(5):
        _task(session, fid=f"FR{i}.T1", parent=f"FR{i}", criteria=["x"], order=i)
    batch = svc.claim_ready_batch(session.id, "frontend", 2)
    assert len(batch) == 2
    # The remaining three are still pending.
    pending = [t for t in svc.session_tasks(session.id) if t.status == DevTaskStatus.PENDING]
    assert len(pending) == 3


def test_batch_respects_dependencies(app):
    session = _session(_project(str(uuid.uuid4())))
    _task(session, fid="FR1.T1", parent="FR1", criteria=["a"], order=1)
    _task(session, fid="FR2.T1", parent="FR2", criteria=["b"], deps=["FR1.T1"], order=2)
    batch = svc.claim_ready_batch(session.id, "frontend", 4)
    assert [t.feature_id for t in batch] == ["FR1.T1"]  # dependant not yet ready


# --- apply_batch_verify_outcomes (sibling-AC masking) ---------------------------
def test_batch_fold_masks_sibling_ac_regression(app):
    session = _session(_project(str(uuid.uuid4())))
    a = _task(session, fid="FR1.T1", parent="FR1", criteria=["渲染列表"])
    b = _task(session, fid="FR2.T1", parent="FR2", criteria=["提交表单"])
    c = _task(session, fid="FR3.T1", parent="FR3", criteria=["显示详情"])
    for t in (a, b, c):
        svc.mark_in_progress(t.id, "run-x")
        svc.mark_verifying(t.id)
    tasks = [db.session.get(CodeDevTask, t.id) for t in (a, b, c)]
    # Merged review over the UNION of all AC: A & C pass, B's single AC fails.
    feats = []
    for t in tasks:
        feats.extend(svc.ac_feature_items(t))
    for f in feats:
        f["passes"] = not f["id"].startswith("FR2")
    outcomes = svc.apply_batch_verify_outcomes(tasks, "run-x", feats, False, "mixed")
    by_id = {o["task_id"]: o for o in outcomes}
    # A and C close DONE and are NOT dragged down by B's failed AC (no false regression).
    assert by_id[a.id]["status"] == DevTaskStatus.DONE and by_id[a.id]["passed"]
    assert by_id[c.id]["status"] == DevTaskStatus.DONE
    assert by_id[a.id]["regressed"] == [] and by_id[c.id]["regressed"] == []
    # B fails its own AC → retryable (back to pending, within budget).
    assert by_id[b.id]["status"] == DevTaskStatus.PENDING and not by_id[b.id]["passed"]
    db.session.expire_all()
    assert db.session.get(CodeDevTask, a.id).status == DevTaskStatus.DONE
    assert db.session.get(CodeDevTask, b.id).status == DevTaskStatus.PENDING


def test_batch_shared_blocker_spares_innocents_without_retry(app):
    """Fix②: a batch-level objective blocker (broken merged tree) must NOT mark any task
    DONE, but tasks whose OWN AC are clean are re-queued WITHOUT burning retry — even at
    max_retries=0 they go pending (not blocked), so one bad lane can't block the rest."""
    session = _session(_project(str(uuid.uuid4())))
    a = _task(session, fid="FR1.T1", parent="FR1", criteria=["ok"], max_retries=0)
    b = _task(session, fid="FR2.T1", parent="FR2", criteria=["ok"], max_retries=0)
    for t in (a, b):
        svc.mark_in_progress(t.id, "run-y")
        svc.mark_verifying(t.id)
    tasks = [db.session.get(CodeDevTask, t.id) for t in (a, b)]
    feats = []
    for t in tasks:
        feats.extend(svc.ac_feature_items(t))
    for f in feats:
        f["passes"] = True  # every AC passes...
    # ...but a shared blocker broke the merged tree → none DONE, none blocked, none charged.
    outcomes = svc.apply_batch_verify_outcomes(tasks, "run-y", feats, True, "运行时错误")
    assert all(o["status"] == DevTaskStatus.PENDING and o.get("shared_block") for o in outcomes)
    db.session.expire_all()
    for t in (a, b):
        row = db.session.get(CodeDevTask, t.id)
        assert row.status == DevTaskStatus.PENDING and row.effective_retry_count == 0


def test_shared_blocking_default_off_counts_retry(app):
    """Serial path (shared_blocking defaults False) is byte-for-byte: a blocker on an
    otherwise-clean task still burns a retry, exactly as before Fix②."""
    session = _session(_project(str(uuid.uuid4())))
    t = _task(session, fid="FR1.T1", criteria=["渲染"], max_retries=2)
    svc.mark_in_progress(t.id, "r")
    svc.mark_verifying(t.id)
    t = db.session.get(CodeDevTask, t.id)
    feats = svc.ac_feature_items(t)
    for f in feats:
        f["passes"] = True
    out = svc.apply_verify_outcome(t, "r", feats, True, "运行时错误")  # no shared_blocking kwarg
    assert out["status"] == DevTaskStatus.PENDING and not out.get("shared_block")
    db.session.expire_all()
    assert db.session.get(CodeDevTask, t.id).effective_retry_count == 1  # burned, as before


def test_shared_blocking_own_ac_fail_still_charges_retry(app):
    """Fix②'s spare applies ONLY to innocent tasks: one whose OWN AC failed is genuinely
    at fault and burns a retry even under a shared blocker (not a free re-queue)."""
    session = _session(_project(str(uuid.uuid4())))
    t = _task(session, fid="FR1.T1", criteria=["渲染"], max_retries=2)
    svc.mark_in_progress(t.id, "r")
    svc.mark_verifying(t.id)
    t = db.session.get(CodeDevTask, t.id)
    feats = svc.ac_feature_items(t)  # passes stay False → own AC failed
    out = svc.apply_verify_outcome(t, "r", feats, True, "x", shared_blocking=True)
    assert out["status"] == DevTaskStatus.PENDING and not out.get("shared_block")
    db.session.expire_all()
    assert db.session.get(CodeDevTask, t.id).effective_retry_count == 1  # burned (own fault)


# --- the parallel scheduling loop (batch child faked) ---------------------------
def _sprint_fixture(session, max_turns=10):
    sprint = CodeDevSprint(
        project_id=session.project_id, session_id=session.id, lane=session.lane,
        status=DevSprintStatus.PLANNED, max_turns=max_turns, created_by=session.user_id,
    )
    db.session.add(sprint)
    db.session.commit()
    run = AgentRun(
        user_id=session.user_id, domain="code", workflow="code_dev_sprint",
        resource_type="code_project", resource_id=session.project_id,
        status=AgentRunStatus.RUNNING,
    )
    run.set_config({"session_id": session.id, "sprint_id": sprint.id})
    db.session.add(run)
    db.session.commit()
    sprint.run_id = run.id
    db.session.commit()
    return sprint, run


def _run_sprint(app, run, monkeypatch, fake_turn):
    from backend.services.agent.recorder import RunRecorder
    from backend.services.agent.runtime import agent_runtime
    from backend.services.agent.schemas import AgentContext
    from backend.services.agent.workflows.code_dev_sprint_workflow import (
        run_code_dev_sprint_workflow,
    )

    monkeypatch.setattr(agent_runtime, "run_sync", fake_turn)
    ctx = AgentContext(
        run_id=run.id, user_id=run.user_id, team_id=None, domain="code",
        workflow="code_dev_sprint", resource_type="code_project",
        resource_id=run.resource_id, config=run.get_config(),
        input_snapshot={}, is_cancelled=lambda: False,
    )
    return run_code_dev_sprint_workflow(ctx, RunRecorder(run.id))


def _passing_batch_turn(round_log, passes=True):
    """Fake the code_dev_parallel_turn child: settle every task in the batch via the
    real state machine (mirrors the workflow's prepare + apply_batch_verify_outcomes)."""

    def fake(app_, run_id):
        run = db.session.get(AgentRun, run_id)
        cfg = run.get_config()
        task_ids = [tc["task_id"] for tc in cfg["tasks"]]
        round_log.append(list(task_ids))
        batch = []
        for tid in task_ids:
            svc.mark_in_progress(tid, run_id)
            svc.mark_verifying(tid)
            batch.append(db.session.get(CodeDevTask, tid))
        feats = []
        for t in batch:
            feats.extend(svc.ac_feature_items(t))
        for f in feats:
            f["passes"] = passes
        svc.apply_batch_verify_outcomes(batch, run_id, feats, False, "ok")
        run.status = AgentRunStatus.COMPLETED
        db.session.commit()

    return fake


def test_parallel_loop_settles_batch_in_one_round(app, monkeypatch):
    monkeypatch.setenv("CODE_DEV_SPRINT_PARALLEL", "1")
    monkeypatch.setenv("CODE_DEV_SPRINT_BATCH", "4")
    session = _session(_project(str(uuid.uuid4())))
    # Two independent ready tasks (different parents) → claimed together in ONE round.
    _task(session, fid="FR1.T1", parent="FR1", criteria=["a"], order=1)
    _task(session, fid="FR2.T1", parent="FR2", criteria=["b"], order=2)
    sprint, run = _sprint_fixture(session)

    rounds: list = []
    result = _run_sprint(app, run, monkeypatch, _passing_batch_turn(rounds))
    assert result["status"] == AgentRunStatus.COMPLETED
    assert len(rounds) == 1 and set(rounds[0]) == {
        t.id for t in svc.session_tasks(session.id)
    }
    db.session.expire_all()
    sprint = db.session.get(CodeDevSprint, sprint.id)
    assert sprint.status == DevSprintStatus.COMPLETED
    assert sprint.turn_count == 1  # a batch of 2 = ONE scheduling round
    assert {t.feature_id: t.status for t in svc.session_tasks(session.id)} == {
        "FR1.T1": DevTaskStatus.DONE, "FR2.T1": DevTaskStatus.DONE,
    }


def test_parallel_loop_infra_fail_requeues_without_retry(app, monkeypatch):
    monkeypatch.setenv("CODE_DEV_SPRINT_PARALLEL", "1")
    session = _session(_project(str(uuid.uuid4())))
    _task(session, fid="FR1.T1", parent="FR1", criteria=["a"], order=1)
    _task(session, fid="FR2.T1", parent="FR2", criteria=["b"], order=2)
    sprint, run = _sprint_fixture(session)

    def crashing_batch(app_, run_id):
        r = db.session.get(AgentRun, run_id)
        for tc in r.get_config()["tasks"]:
            svc.mark_in_progress(tc["task_id"], run_id)  # left ACTIVE by the crash
        r.status = AgentRunStatus.FAILED
        r.error_message = "容器崩溃"
        db.session.commit()

    with pytest.raises(RuntimeError):  # two consecutive infra fails abort the sprint
        _run_sprint(app, run, monkeypatch, crashing_batch)
    db.session.expire_all()
    # Every task is back to pending and NONE burned a retry (systemic outage).
    for t in svc.session_tasks(session.id):
        assert t.status == DevTaskStatus.PENDING
        assert t.effective_retry_count == 0
    assert db.session.get(CodeDevSprint, sprint.id).status == DevSprintStatus.FAILED


def test_parallel_off_uses_single_claim(app, monkeypatch):
    """Flag OFF ⇒ the loop must go through claim_next_task, never claim_ready_batch."""
    monkeypatch.delenv("CODE_DEV_SPRINT_PARALLEL", raising=False)
    session = _session(_project(str(uuid.uuid4())))
    _task(session, fid="FR1.T1", parent="FR1", criteria=["a"], order=1)
    sprint, run = _sprint_fixture(session)

    calls = {"batch": 0, "single": 0}
    real_single = svc.claim_next_task

    def spy_batch(*a, **k):
        calls["batch"] += 1
        return svc.claim_ready_batch(*a, **k)

    def spy_single(*a, **k):
        calls["single"] += 1
        return real_single(*a, **k)

    monkeypatch.setattr(svc, "claim_ready_batch", spy_batch)
    monkeypatch.setattr(svc, "claim_next_task", spy_single)

    def single_turn(app_, run_id):
        r = db.session.get(AgentRun, run_id)
        cfg = r.get_config()
        tid = cfg["task_id"]  # single path config carries task_id, NOT tasks
        svc.mark_in_progress(tid, run_id)
        svc.mark_verifying(tid)
        task = db.session.get(CodeDevTask, tid)
        feats = svc.features_from_dev_tasks(cfg["session_id"], focus_task=task)
        for f in feats:
            f["passes"] = True
        svc.apply_verify_outcome(task, run_id, feats, False, "ok")
        r.status = AgentRunStatus.COMPLETED
        db.session.commit()

    result = _run_sprint(app, run, monkeypatch, single_turn)
    assert result["status"] == AgentRunStatus.COMPLETED
    assert calls["single"] >= 1 and calls["batch"] == 0
