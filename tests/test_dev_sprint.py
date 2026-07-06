"""
Unit tests for the Dev Mode sprint scheduler (P0: DB task state machine + serial
sprint) — network/docker-free.

Covers: workflow/pricing registration, the atomic task transitions (claim races,
retry/block budgets), ready-derivation over dependencies (incl. dead-dep
auto-block), per-task acceptance folding (apply_verify_outcome), the task brief,
stale-claim reconcile, the serial scheduling loop end-to-end (child turns faked
via a monkeypatched ``agent_runtime.run_sync``), and the HTTP surface
(tasks/bulk + sprint create/pause/resume/cancel).
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
def app(tmp_path, monkeypatch):
    application = create_app("testing")
    application.config["UPLOAD_FOLDER"] = str(tmp_path / "uploads")
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


def _project(user_id: str) -> CodeProject:
    project = CodeProject(user_id=user_id, title="Sprint Test", requirement_input="做一个订单系统")
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
          retry_count=None) -> CodeDevTask:
    t = CodeDevTask(
        project_id=session.project_id, session_id=session.id, feature_id=fid,
        category=category, lane=lane, title=title, status=status,
        priority=priority, max_retries=max_retries, order_index=order,
        retry_count=retry_count,
    )
    if deps is not None:
        t.set_depends_on(deps)
    if criteria is not None:
        t.set_acceptance_criteria(criteria)
    db.session.add(t)
    db.session.commit()
    return t


# --- registration / pricing --------------------------------------------------
def test_sprint_workflow_registered(app):
    from backend.routes.agent_routes import WORKFLOW_COSTS
    from backend.services import pricing
    from backend.services.agent.runtime import RESUME_FROM_SCRATCH, known_workflows

    assert "code_dev_sprint" in known_workflows()
    assert "code_dev_sprint" in WORKFLOW_COSTS
    assert pricing.CODE_DEV_SPRINT == 0
    assert pricing.OPERATION["code_dev_sprint"] == ("agent_run", 0)
    # Stateless orchestrator — must survive a service restart by re-entering.
    assert "code_dev_sprint" in RESUME_FROM_SCRATCH


# --- atomic transitions -------------------------------------------------------
def test_claim_is_atomic_single_winner(app):
    session = _session(_project(str(uuid.uuid4())))
    t = _task(session, fid="FR1.T1")
    assert svc.mark_queued(t.id) is True
    # Second claim loses the race (row already queued).
    assert svc.mark_queued(t.id) is False


def test_full_happy_path_transitions(app):
    session = _session(_project(str(uuid.uuid4())))
    t = _task(session, fid="FR1.T1", criteria=["能创建订单"])
    assert svc.mark_queued(t.id)
    assert svc.mark_in_progress(t.id, "run-1")
    assert svc.mark_verifying(t.id)
    db.session.expire_all()
    row = db.session.get(CodeDevTask, t.id)
    assert row.status == DevTaskStatus.VERIFYING
    assert row.last_attempt_run_id == "run-1"
    # verifying -> done via a fully-passing verification.
    feats = svc.features_from_dev_tasks(session.id, focus_task=row)
    for f in feats:
        f["passes"] = True
    outcome = svc.apply_verify_outcome(row, "run-1", feats, False, "ok")
    assert outcome["status"] == DevTaskStatus.DONE
    db.session.expire_all()
    row = db.session.get(CodeDevTask, t.id)
    assert row.status == DevTaskStatus.DONE
    assert row.origin_turn_run_id == "run-1"


def test_verify_fail_retries_then_blocks(app):
    session = _session(_project(str(uuid.uuid4())))
    t = _task(session, fid="FR1.T1", criteria=["A", "B"], max_retries=1)
    svc.mark_queued(t.id)
    svc.mark_in_progress(t.id, "run-1")
    svc.mark_verifying(t.id)
    row = db.session.get(CodeDevTask, t.id)
    feats = svc.features_from_dev_tasks(session.id, focus_task=row)  # all passes=False
    outcome = svc.apply_verify_outcome(row, "run-1", feats, False)
    assert outcome["status"] == DevTaskStatus.PENDING  # retry budget left
    db.session.expire_all()
    row = db.session.get(CodeDevTask, t.id)
    assert row.status == DevTaskStatus.PENDING
    assert row.effective_retry_count == 1
    assert "未通过的验收标准" in (row.note or "")
    # Second failing attempt exhausts the budget -> blocked.
    svc.mark_queued(t.id)
    svc.mark_in_progress(t.id, "run-2")
    svc.mark_verifying(t.id)
    row = db.session.get(CodeDevTask, t.id)
    outcome = svc.apply_verify_outcome(row, "run-2", feats, False)
    assert outcome["status"] == DevTaskStatus.BLOCKED
    db.session.expire_all()
    row = db.session.get(CodeDevTask, t.id)
    assert row.status == DevTaskStatus.BLOCKED
    assert "重试" in (row.blocked_reason or "")


def test_regression_on_done_task_prevents_done(app):
    session = _session(_project(str(uuid.uuid4())))
    done = _task(session, fid="FR1.T1", title="登录", status=DevTaskStatus.DONE)
    t = _task(session, fid="FR1.T2", criteria=["能筛选"], max_retries=2)
    svc.mark_queued(t.id)
    svc.mark_in_progress(t.id, "run-1")
    svc.mark_verifying(t.id)
    row = db.session.get(CodeDevTask, t.id)
    feats = svc.features_from_dev_tasks(session.id, focus_task=row)
    for f in feats:
        f["passes"] = f["id"] != "FR1.T1"  # own AC pass, but the DONE task regressed
    outcome = svc.apply_verify_outcome(row, "run-1", feats, False)
    assert outcome["status"] == DevTaskStatus.PENDING
    assert "FR1.T1" in outcome["regressed"]
    # The regressed done-task itself stays done on the board (surfaced, not reset).
    assert db.session.get(CodeDevTask, done.id).status == DevTaskStatus.DONE


def test_task_turn_scoped_to_own_ac_closes_despite_sibling_done(app):
    """The turn now verifies a task on its OWN AC only (ac_feature_items) — a sibling
    DONE task is NOT dragged in as a regression item, so a truncated-digest false
    regression on it can no longer block this task. All own AC passing -> DONE."""
    session = _session(_project(str(uuid.uuid4())))
    _task(session, fid="FR1.T1", title="登录", status=DevTaskStatus.DONE)
    t = _task(session, fid="FR1.T2", criteria=["能筛选", "能分页"], max_retries=2)
    svc.mark_queued(t.id)
    svc.mark_in_progress(t.id, "run-1")
    svc.mark_verifying(t.id)
    row = db.session.get(CodeDevTask, t.id)
    feats = svc.ac_feature_items(row)  # what the turn now feeds — own AC only
    assert {f["id"] for f in feats} == {"FR1.T2.AC1", "FR1.T2.AC2"}  # no sibling FR1.T1
    for f in feats:
        f["passes"] = True
    outcome = svc.apply_verify_outcome(row, "run-1", feats, False)
    assert outcome["status"] == DevTaskStatus.DONE
    assert outcome["regressed"] == []


# --- ready derivation / deps ---------------------------------------------------
def test_ready_respects_dependencies_and_priority(app):
    session = _session(_project(str(uuid.uuid4())))
    _task(session, fid="FR1.T1", title="基础", status=DevTaskStatus.DONE, order=1)
    t2 = _task(session, fid="FR1.T2", deps=["FR1.T1"], order=2)
    t3 = _task(session, fid="FR2.T1", deps=["FR9.T9"], order=3)  # waiting? no: FR9 missing -> dead
    t4 = _task(session, fid="FR3.T1", order=4, priority=5)
    t5 = _task(session, fid="ASSET.1", category="asset", lane="asset", order=5, priority=5)

    ready = svc.ready_tasks(session.id, "frontend")
    ids = [t.feature_id for t in ready]
    # dead-dep task not ready; deps-done + no-dep tasks are; priority 5 first,
    # asset before functional at equal priority.
    assert t3.feature_id not in ids
    assert ids[0] == "ASSET.1" and ids[1] == "FR3.T1"
    assert "FR1.T2" in ids
    assert t2.feature_id in ids and t4.feature_id in ids and t5.feature_id in ids


def test_dead_dependency_autoblocks(app):
    session = _session(_project(str(uuid.uuid4())))
    _task(session, fid="FR1.T1", status=DevTaskStatus.SKIPPED)
    t_dead = _task(session, fid="FR1.T2", deps=["FR1.T1"])  # dep skipped -> unsatisfiable
    t_missing = _task(session, fid="FR2.T1", deps=["NOPE"])
    t_wait = _task(session, fid="FR3.T1", deps=["FR3.T0"])
    _task(session, fid="FR3.T0", status=DevTaskStatus.PENDING)

    blocked = svc.block_dead_dependency_tasks(session.id)
    assert set(blocked) == {t_dead.id, t_missing.id}
    db.session.expire_all()
    assert db.session.get(CodeDevTask, t_dead.id).status == DevTaskStatus.BLOCKED
    assert "依赖不可满足" in db.session.get(CodeDevTask, t_dead.id).blocked_reason
    assert "依赖缺失" in db.session.get(CodeDevTask, t_missing.id).blocked_reason
    # A merely-waiting dep must NOT block.
    assert db.session.get(CodeDevTask, t_wait.id).status == DevTaskStatus.PENDING


def test_claim_next_task_skips_backend_lane_for_frontend_session(app):
    session = _session(_project(str(uuid.uuid4())))
    _task(session, fid="BE.T1", lane="backend")
    assert svc.claim_next_task(session.id, "frontend") is None
    fe = _task(session, fid="FE.T1", lane="frontend")
    claimed = svc.claim_next_task(session.id, "frontend")
    assert claimed is not None and claimed.id == fe.id
    assert claimed.status == DevTaskStatus.QUEUED


# --- features / brief -----------------------------------------------------------
def test_features_from_dev_tasks_shape(app):
    session = _session(_project(str(uuid.uuid4())))
    _task(session, fid="FR1.T1", title="登录", status=DevTaskStatus.DONE)
    _task(session, fid="FR9.T9", title="未做的兄弟任务")  # pending sibling — excluded
    focus = _task(session, fid="FR1.T2", criteria=["能筛选", "能分页"])
    feats = svc.features_from_dev_tasks(session.id, focus_task=focus)
    by_id = {f["id"]: f for f in feats}
    assert set(by_id) == {"FR1.T2.AC1", "FR1.T2.AC2", "FR1.T1"}
    assert by_id["FR1.T2.AC1"]["passes"] is False  # focus starts failing
    assert by_id["FR1.T1"]["passes"] is True  # done task seeds passing (regression set)


def test_build_task_brief_contents(app):
    session = _session(_project(str(uuid.uuid4())))
    _task(session, fid="FR1.T1", title="登录", status=DevTaskStatus.DONE)
    t = _task(
        session, fid="FR1.T2", title="订单筛选", deps=["FR1.T1"],
        criteria=["可按状态筛选", "分页读取 data.items"],
    )
    brief = svc.build_task_brief(t, {"FR1.T1": "登录"})
    assert "任务 ID: FR1.T2" in brief
    assert "可按状态筛选" in brief and "分页读取 data.items" in brief
    assert "FR1.T1 已完成" in brief
    assert "禁止事项" in brief and "不要重写" in brief
    # Retry feedback appears only on a retry attempt.
    assert "上次尝试未通过的原因" not in brief
    t.retry_count = 1
    t.note = "筛选未生效"
    db.session.commit()
    assert "筛选未生效" in svc.build_task_brief(t, {})


def test_asset_task_brief_lists_outputs(app):
    session = _session(_project(str(uuid.uuid4())))
    t = _task(session, fid="ASSET.FR2.1", title="生成订单头图", category="asset", lane="asset")
    t.set_resource_spec({
        "skill": "image-assets",
        "outputs": [{"path": "src/assets/order-hero.png", "size": "1536x1024"}],
    })
    db.session.commit()
    brief = svc.build_task_brief(t, {})
    assert "image-assets" in brief
    assert "src/assets/order-hero.png (1536x1024)" in brief
    assert "非 0 字节" in brief


# --- reconcile -------------------------------------------------------------------
def test_reconcile_stale_tasks_retries_then_fails(app):
    session = _session(_project(str(uuid.uuid4())))
    t1 = _task(session, fid="FR1.T1", status=DevTaskStatus.IN_PROGRESS)  # no run at all
    t2 = _task(session, fid="FR1.T2", status=DevTaskStatus.VERIFYING, max_retries=0)
    healed = svc.reconcile_stale_tasks(session.id)
    assert set(healed) == {t1.id, t2.id}
    db.session.expire_all()
    assert db.session.get(CodeDevTask, t1.id).status == DevTaskStatus.PENDING
    assert db.session.get(CodeDevTask, t1.id).effective_retry_count == 1
    assert db.session.get(CodeDevTask, t2.id).status == DevTaskStatus.FAILED


def test_reconcile_leaves_live_run_alone(app):
    session = _session(_project(str(uuid.uuid4())))
    run = AgentRun(
        user_id=session.user_id, domain="code", workflow="code_dev_turn",
        resource_type="code_project", resource_id=session.project_id,
        status=AgentRunStatus.RUNNING,
    )
    db.session.add(run)
    db.session.commit()
    t = _task(session, fid="FR1.T1", status=DevTaskStatus.IN_PROGRESS)
    t.last_attempt_run_id = run.id
    db.session.commit()
    assert svc.reconcile_stale_tasks(session.id) == []
    assert db.session.get(CodeDevTask, t.id).status == DevTaskStatus.IN_PROGRESS


# --- the serial scheduling loop (child turns faked) -------------------------------
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


def _passing_fake_turn(order_log):
    """A fake child turn: marks the task done via the real state machine."""

    def fake(app, run_id):
        run = db.session.get(AgentRun, run_id)
        cfg = run.get_config()
        task_id = cfg["task_id"]
        svc.mark_in_progress(task_id, run_id)
        svc.mark_verifying(task_id)
        task = db.session.get(CodeDevTask, task_id)
        order_log.append(task.feature_id)
        feats = svc.features_from_dev_tasks(cfg["session_id"], focus_task=task)
        for f in feats:
            f["passes"] = True
        svc.apply_verify_outcome(task, run_id, feats, False, "ok")
        run.status = AgentRunStatus.COMPLETED
        db.session.commit()

    return fake


def test_sprint_serial_loop_completes_in_dependency_order(app, monkeypatch):
    session = _session(_project(str(uuid.uuid4())))
    _task(session, fid="FR1.T1", title="基础", order=1)
    _task(session, fid="FR1.T2", title="进阶", deps=["FR1.T1"], order=2)
    sprint, run = _sprint_fixture(session)

    order_log: list = []
    result = _run_sprint(app, run, monkeypatch, _passing_fake_turn(order_log))
    assert result["status"] == AgentRunStatus.COMPLETED
    # Dependency order honored; each task exactly one turn.
    assert order_log == ["FR1.T1", "FR1.T2"]
    db.session.expire_all()
    sprint = db.session.get(CodeDevSprint, sprint.id)
    assert sprint.status == DevSprintStatus.COMPLETED
    assert sprint.turn_count == 2
    assert sprint.finished_at is not None
    statuses = {t.feature_id: t.status for t in svc.session_tasks(session.id)}
    assert statuses == {"FR1.T1": DevTaskStatus.DONE, "FR1.T2": DevTaskStatus.DONE}


def test_sprint_blocks_when_task_exhausts_retries(app, monkeypatch):
    session = _session(_project(str(uuid.uuid4())))
    _task(session, fid="FR1.T1", criteria=["永不通过"], max_retries=1)
    sprint, run = _sprint_fixture(session)

    def failing_turn(app_, run_id):
        run_ = db.session.get(AgentRun, run_id)
        cfg = run_.get_config()
        svc.mark_in_progress(cfg["task_id"], run_id)
        svc.mark_verifying(cfg["task_id"])
        task = db.session.get(CodeDevTask, cfg["task_id"])
        feats = svc.features_from_dev_tasks(cfg["session_id"], focus_task=task)
        svc.apply_verify_outcome(task, run_id, feats, False)  # all AC fail
        run_.status = AgentRunStatus.COMPLETED
        db.session.commit()

    result = _run_sprint(app, run, monkeypatch, failing_turn)
    assert result["status"] == AgentRunStatus.COMPLETED
    db.session.expire_all()
    sprint = db.session.get(CodeDevSprint, sprint.id)
    assert sprint.status == DevSprintStatus.BLOCKED
    assert sprint.turn_count == 2  # initial attempt + one retry
    task = svc.session_tasks(session.id)[0]
    assert task.status == DevTaskStatus.BLOCKED


def test_sprint_pausing_parks_as_paused(app, monkeypatch):
    session = _session(_project(str(uuid.uuid4())))
    _task(session, fid="FR1.T1")
    sprint, run = _sprint_fixture(session)
    sprint.status = DevSprintStatus.PAUSING
    db.session.commit()

    result = _run_sprint(app, run, monkeypatch, _passing_fake_turn([]))
    assert result["status"] == AgentRunStatus.PAUSED
    db.session.expire_all()
    assert db.session.get(CodeDevSprint, sprint.id).status == DevSprintStatus.PAUSED
    # Nothing was scheduled — the backlog is intact for the resume.
    assert svc.session_tasks(session.id)[0].status == DevTaskStatus.PENDING


def test_sprint_consecutive_run_failures_fail_the_sprint(app, monkeypatch):
    session = _session(_project(str(uuid.uuid4())))
    _task(session, fid="FR1.T1", max_retries=5)
    _task(session, fid="FR2.T1", max_retries=5)
    sprint, run = _sprint_fixture(session)

    def crashing_turn(app_, run_id):
        run_ = db.session.get(AgentRun, run_id)
        run_.status = AgentRunStatus.FAILED
        run_.error_message = "container crashed"
        db.session.commit()

    with pytest.raises(RuntimeError):
        _run_sprint(app, run, monkeypatch, crashing_turn)
    db.session.expire_all()
    assert db.session.get(CodeDevSprint, sprint.id).status == DevSprintStatus.FAILED
    # Infra failure marks the attempted tasks failed (design: in_progress -> failed).
    statuses = [t.status for t in svc.session_tasks(session.id)]
    assert DevTaskStatus.FAILED in statuses


# --- HTTP surface ------------------------------------------------------------------
def _client_and_token(app, user_id):
    from flask_jwt_extended import create_access_token

    return app.test_client(), create_access_token(identity=user_id)


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_bulk_tasks_insert_upsert_and_replace_guard(app, monkeypatch):
    uid = str(uuid.uuid4())
    project = _project(uid)
    session = _session(project)
    client, token = _client_and_token(app, uid)
    url = f"/api/code/projects/{project.id}/dev-sessions/{session.id}/tasks/bulk"

    resp = client.post(url, json={"tasks": [
        {"feature_id": "FR1.T1", "title": "登录", "acceptance_criteria": ["能登录"],
         "priority": 3, "category": "functional"},
        {"feature_id": "ASSET.1", "title": "头图", "category": "asset", "lane": "asset",
         "resource_spec": {"skill": "image-assets", "outputs": [{"path": "a.png"}]}},
    ]}, headers=_auth(token))
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["inserted"] == 2

    # Upsert by feature_id updates a pending row; a DONE row is protected.
    row = CodeDevTask.query.filter_by(session_id=session.id, feature_id="FR1.T1").first()
    row.status = DevTaskStatus.DONE
    db.session.commit()
    resp = client.post(url, json={"tasks": [
        {"feature_id": "FR1.T1", "title": "改标题"},
        {"feature_id": "ASSET.1", "title": "新头图", "priority": 9},
    ]}, headers=_auth(token))
    data = resp.get_json()["data"]
    assert data["skipped"] == 1 and data["updated"] == 1
    db.session.expire_all()
    assert CodeDevTask.query.filter_by(feature_id="FR1.T1").first().title == "登录"
    assert CodeDevTask.query.filter_by(feature_id="ASSET.1").first().priority == 9

    # Replace is refused while a sprint is active.
    sprint = CodeDevSprint(
        project_id=project.id, session_id=session.id, lane="frontend",
        status=DevSprintStatus.RUNNING, created_by=uid,
    )
    db.session.add(sprint)
    db.session.commit()
    resp = client.post(url, json={"tasks": [{"title": "x"}], "replace": True}, headers=_auth(token))
    assert resp.status_code == 400


def test_sprint_api_lifecycle(app, monkeypatch):
    from backend.services.agent.runtime import agent_runtime

    started: list = []
    monkeypatch.setattr(agent_runtime, "start", lambda app_, run_id: started.append(run_id))

    uid = str(uuid.uuid4())
    project = _project(uid)
    session = _session(project)
    _task(session, fid="FR1.T1")
    client, token = _client_and_token(app, uid)
    base = f"/api/code/projects/{project.id}/dev-sessions/{session.id}/sprints"

    # Parallel mode is P3 — refused for now.
    resp = client.post(base, json={"mode": "parallel"}, headers=_auth(token))
    assert resp.status_code == 400

    resp = client.post(base, json={"max_turns": 8}, headers=_auth(token))
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    sprint_id = data["sprint"]["id"]
    assert data["sprint"]["status"] == DevSprintStatus.PLANNED
    assert data["sprint"]["max_turns"] == 8
    assert data["run_id"] and started == [data["run_id"]]

    # Only one live sprint per session.
    resp = client.post(base, json={}, headers=_auth(token))
    assert resp.status_code == 400

    # GET view.
    resp = client.get(f"{base}/{sprint_id}", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.get_json()["data"]["sprint"]["id"] == sprint_id

    # Pause -> pausing; resume (withdraw) -> running again.
    db.session.get(CodeDevSprint, sprint_id).status = DevSprintStatus.RUNNING
    db.session.commit()
    resp = client.post(f"{base}/{sprint_id}/pause", headers=_auth(token))
    assert resp.get_json()["data"]["sprint"]["status"] == DevSprintStatus.PAUSING
    resp = client.post(f"{base}/{sprint_id}/resume", headers=_auth(token))
    assert resp.get_json()["data"]["sprint"]["status"] == DevSprintStatus.RUNNING

    # Cancel releases the claimed task terminally.
    t = CodeDevTask.query.filter_by(session_id=session.id, feature_id="FR1.T1").first()
    t.status = DevTaskStatus.IN_PROGRESS
    sprint = db.session.get(CodeDevSprint, sprint_id)
    sprint.set_current_task_ids([t.id])
    db.session.commit()
    resp = client.post(f"{base}/{sprint_id}/cancel", headers=_auth(token))
    assert resp.get_json()["data"]["sprint"]["status"] == DevSprintStatus.CANCELLED
    db.session.expire_all()
    assert db.session.get(CodeDevTask, t.id).status == DevTaskStatus.CANCELLED
    # Cancel is idempotent.
    resp = client.post(f"{base}/{sprint_id}/cancel", headers=_auth(token))
    assert resp.status_code == 200


def test_sprint_requires_pending_tasks_and_frontend_lane(app, monkeypatch):
    from backend.services.agent.runtime import agent_runtime

    monkeypatch.setattr(agent_runtime, "start", lambda app_, run_id: None)
    uid = str(uuid.uuid4())
    project = _project(uid)
    empty_session = _session(project)
    client, token = _client_and_token(app, uid)
    resp = client.post(
        f"/api/code/projects/{project.id}/dev-sessions/{empty_session.id}/sprints",
        json={}, headers=_auth(token),
    )
    assert resp.status_code == 400  # no pending tasks

    be_session = _session(project, lane="backend")
    _task(be_session, fid="BE.T1", lane="backend")
    resp = client.post(
        f"/api/code/projects/{project.id}/dev-sessions/{be_session.id}/sprints",
        json={}, headers=_auth(token),
    )
    assert resp.status_code == 400  # P0: frontend sessions only


def test_task_patch_rejects_scheduler_states(app):
    uid = str(uuid.uuid4())
    project = _project(uid)
    session = _session(project)
    t = _task(session, fid="FR1.T1", status=DevTaskStatus.BLOCKED)
    t.blocked_reason = "重试用尽"
    db.session.commit()
    client, token = _client_and_token(app, uid)
    url = f"/api/code/projects/{project.id}/dev-tasks/{t.id}"
    # Scheduler-owned states are not user-settable.
    resp = client.patch(url, json={"status": "verifying"}, headers=_auth(token))
    assert resp.status_code == 400
    # Setting pending clears the block (the manual "unblock + requeue" path).
    resp = client.patch(url, json={"status": "pending"}, headers=_auth(token))
    assert resp.status_code == 200
    db.session.expire_all()
    row = db.session.get(CodeDevTask, t.id)
    assert row.status == DevTaskStatus.PENDING
    assert row.blocked_reason is None
