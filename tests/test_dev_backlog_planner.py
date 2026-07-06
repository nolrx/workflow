"""
Unit tests for the Dev Mode backlog planner (P1) — network-free.

Covers: workflow/pricing registration, plan JSON parsing (fence-tolerant),
normalization (dedupe / lane filter / dependency resolution / cycle breaking /
asset output safety), the deterministic fallback, fingerprint-guarded apply
(stale refusal + force), bulk-write protections (done/active never clobbered,
replace refused under an active sprint), and the HTTP surface.
"""
import uuid

import pytest

from backend.app import create_app
from backend.extensions import db
from backend.models.code import CodeProject
from backend.models.code.fullstack import (
    CodeDevSession,
    CodeDevSprint,
    CodeDevTask,
    CodeDevTaskPlan,
    DevSessionStatus,
    DevSprintStatus,
    DevTaskPlanStatus,
    DevTaskSource,
    DevTaskStatus,
)
from backend.services.code import dev_backlog_planner_service as planner
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
    p = CodeProject(
        user_id=user_id, title="Planner Test", requirement_input="做一个订单系统",
        requirements_doc="# 需求\n- FR1 订单列表\n- FR2 订单详情",
    )
    db.session.add(p)
    db.session.commit()
    return p


def _session(project: CodeProject, lane="frontend") -> CodeDevSession:
    s = CodeDevSession(
        project_id=project.id, user_id=project.user_id, lane=lane,
        status=DevSessionStatus.RUNNING,
    )
    s.set_shared_ledger({
        "requirements": [
            {"id": "FR-01", "statement": "用户可以查看订单列表"},
            {"id": "FR-02", "statement": "用户可以查看订单详情"},
        ]
    })
    db.session.add(s)
    db.session.commit()
    return s


def _plan_row(session, status=DevTaskPlanStatus.PLANNING) -> CodeDevTaskPlan:
    row = CodeDevTaskPlan(
        project_id=session.project_id, session_id=session.id,
        status=status, created_by=session.user_id,
    )
    db.session.add(row)
    db.session.commit()
    return row


# --- registration --------------------------------------------------------------
def test_planner_registered(app):
    from backend.routes.agent_routes import WORKFLOW_COSTS
    from backend.services import pricing
    from backend.services.agent.runtime import known_workflows

    assert "code_dev_backlog_planner" in known_workflows()
    assert "code_dev_backlog_planner" in WORKFLOW_COSTS
    assert pricing.CODE_DEV_BACKLOG_PLANNER == 0
    assert pricing.OPERATION["code_dev_backlog_planner"] == ("agent_run", 0)
    assert pricing.OPERATION["code_dev_asset_image"] == ("agent_run", 0)


# --- parsing --------------------------------------------------------------------
def test_parse_plan_json_plain_and_fenced():
    plain = '{"version": "dev-backlog-plan.v1", "tasks": []}'
    assert planner.parse_plan_json(plain)["version"] == "dev-backlog-plan.v1"
    fenced = "前面是解释文字\n```json\n" + plain + "\n```\n后面还有话"
    assert planner.parse_plan_json(fenced)["version"] == "dev-backlog-plan.v1"
    prose = "答案如下 " + plain + " 完毕"
    assert planner.parse_plan_json(prose)["version"] == "dev-backlog-plan.v1"
    assert planner.parse_plan_json("not json at all") is None
    assert planner.parse_plan_json("") is None


# --- normalization ---------------------------------------------------------------
def test_normalize_dedupes_and_filters_lanes():
    raw = {
        "summary": "s",
        "tasks": [
            {"feature_id": "FR1.T1", "title": "A", "lane": "frontend"},
            {"feature_id": "FR1.T1", "title": "A-dup", "lane": "frontend"},
            {"feature_id": "BE.T1", "title": "后端任务", "lane": "backend"},
            {"title": "没有 id 的任务"},
        ],
    }
    plan, warnings = planner.normalize_plan(raw)
    ids = [t["feature_id"] for t in plan["tasks"]]
    assert ids == ["FR1.T1", "AUTO.T1"]
    assert any("重复 feature_id" in w for w in warnings)
    assert any("lane=backend" in w for w in warnings)


def test_normalize_drops_unknown_deps_and_breaks_cycles():
    raw = {"tasks": [
        {"feature_id": "T1", "title": "一", "depends_on": ["T2", "GHOST"]},
        {"feature_id": "T2", "title": "二", "depends_on": ["T1"]},
    ]}
    plan, warnings = planner.normalize_plan(raw)
    deps = {t["feature_id"]: t["depends_on"] for t in plan["tasks"]}
    # GHOST dropped; the T1<->T2 cycle deterministically broken.
    assert "GHOST" not in deps["T1"]
    assert deps["T1"] == ["T2"] and deps["T2"] == []
    assert any("未知 feature_id=GHOST" in w for w in warnings)
    assert any("依赖环" in w for w in warnings)


def test_normalize_asset_task_output_safety():
    raw = {"tasks": [
        {"feature_id": "ASSET.1", "title": "生成图", "category": "asset",
         "resource_spec": {"outputs": [
             {"path": "src/assets/hero.png", "size": "1536x1024"},
             {"path": "/etc/passwd.png"},
             {"path": "../escape.png"},
             {"path": "public/logo.png"},
             {"path": "https://cdn.example.com/x.png"},
         ]}},
        {"feature_id": "ASSET.2", "title": "全非法", "category": "asset",
         "resource_spec": {"outputs": [{"path": "../nope.png"}]}},
    ]}
    plan, warnings = planner.normalize_plan(raw)
    assert len(plan["tasks"]) == 1
    outputs = plan["tasks"][0]["resource_spec"]["outputs"]
    assert [o["path"] for o in outputs] == ["src/assets/hero.png"]
    assert plan["tasks"][0]["lane"] == "asset"  # asset category defaults the lane
    assert any("没有任何合法 output" in w for w in warnings)


def test_normalize_warns_on_protected_feature_ids(app):
    session = _session(_project(str(uuid.uuid4())))
    done = CodeDevTask(
        project_id=session.project_id, session_id=session.id, feature_id="FR1.T1",
        title="已完成", status=DevTaskStatus.DONE,
    )
    db.session.add(done)
    db.session.commit()
    raw = {"tasks": [{"feature_id": "FR1.T1", "title": "重拆已完成"}]}
    plan, warnings = planner.normalize_plan(raw, existing_tasks=svc.session_tasks(session.id))
    assert any("不会覆盖" in w for w in warnings)


def test_deterministic_fallback_from_ledger(app):
    project = _project(str(uuid.uuid4()))
    session = _session(project)
    context = planner.build_planner_context(project, session)
    raw = planner.deterministic_fallback(context)
    plan, _ = planner.normalize_plan(raw, max_tasks=context["max_tasks"])
    ids = [t["feature_id"] for t in plan["tasks"]]
    assert ids == ["FR-01.T1", "FR-02.T1"]
    assert all(t["acceptance_criteria"] for t in plan["tasks"])
    assert "degraded:fallback" in raw["warnings"]


def test_fallback_ignores_seeded_pending_board(app):
    """Regression: a fresh dev session auto-seeds the board with the ledger's coarse
    FR/NFR (all pending). The fallback must NOT treat those as 'covered' — only DONE
    features are — else it produces an EMPTY plan exactly when the model is down on a
    brand-new session (the production 400 root cause)."""
    project = _project(str(uuid.uuid4()))
    session = _session(project)
    # Simulate seed_checklist: coarse FR-01 pending, FR-02 already DONE.
    db.session.add(CodeDevTask(
        project_id=project.id, session_id=session.id, feature_id="FR-01",
        title="首页", status=DevTaskStatus.PENDING,
    ))
    db.session.add(CodeDevTask(
        project_id=project.id, session_id=session.id, feature_id="FR-02",
        title="详情", status=DevTaskStatus.DONE,
    ))
    db.session.commit()
    context = planner.build_planner_context(project, session)
    raw = planner.deterministic_fallback(context)
    ids = [t["feature_id"] for t in raw["tasks"]]
    # FR-01 (seeded pending) still planned as FR-01.T1; FR-02 (DONE) skipped.
    assert ids == ["FR-01.T1"]


# --- fingerprint / apply ------------------------------------------------------------
def test_fingerprint_changes_with_board(app):
    project = _project(str(uuid.uuid4()))
    session = _session(project)
    fp1 = planner.input_fingerprint(planner.build_planner_context(project, session))
    db.session.add(CodeDevTask(
        project_id=project.id, session_id=session.id, feature_id="X.T1",
        title="新任务", status=DevTaskStatus.PENDING,
    ))
    db.session.commit()
    fp2 = planner.input_fingerprint(planner.build_planner_context(project, session))
    assert fp1 != fp2


def _draft_plan(project, session, tasks) -> CodeDevTaskPlan:
    row = _plan_row(session, status=DevTaskPlanStatus.DRAFT)
    context = planner.build_planner_context(project, session)
    normalized, _ = planner.normalize_plan({"tasks": tasks}, max_tasks=80)
    normalized["request"] = {"instruction": "", "include_assets": True, "max_tasks": 80}
    row.set_plan(normalized)
    row.input_fingerprint = planner.input_fingerprint(context)
    db.session.commit()
    return row


def test_apply_plan_writes_board_with_planner_source(app):
    project = _project(str(uuid.uuid4()))
    session = _session(project)
    row = _draft_plan(project, session, [
        {"feature_id": "FR1.T1", "title": "订单列表", "acceptance_criteria": ["能看到列表"]},
        {"feature_id": "FR1.T2", "title": "订单详情", "depends_on": ["FR1.T1"],
         "planner_meta": {"risk": "low"}},
    ])
    counts = planner.apply_plan(row, project, session)
    assert counts == {"inserted": 2, "updated": 0, "skipped": 0}
    db.session.expire_all()
    assert row.status == DevTaskPlanStatus.APPLIED
    tasks = {t.feature_id: t for t in svc.session_tasks(session.id)}
    assert tasks["FR1.T1"].source == DevTaskSource.PLANNER
    assert tasks["FR1.T1"].plan_id == row.id
    assert tasks["FR1.T2"].get_depends_on() == ["FR1.T1"]
    assert tasks["FR1.T2"].get_planner_meta() == {"risk": "low"}


def test_apply_plan_stale_refused_then_forced(app):
    project = _project(str(uuid.uuid4()))
    session = _session(project)
    row = _draft_plan(project, session, [{"feature_id": "FR9.T1", "title": "任务"}])
    # Drift the board after the plan was generated.
    db.session.add(CodeDevTask(
        project_id=project.id, session_id=session.id, feature_id="DRIFT.T1",
        title="漂移", status=DevTaskStatus.PENDING,
    ))
    db.session.commit()
    with pytest.raises(planner.PlanStale):
        planner.apply_plan(row, project, session)
    db.session.expire_all()
    assert row.status == DevTaskPlanStatus.STALE
    # force applies from stale.
    counts = planner.apply_plan(row, project, session, force=True)
    assert counts["inserted"] == 1
    assert row.status == DevTaskPlanStatus.APPLIED


def test_apply_plan_never_clobbers_done_or_active(app):
    project = _project(str(uuid.uuid4()))
    session = _session(project)
    for fid, status in (("FR1.T1", DevTaskStatus.DONE), ("FR1.T2", DevTaskStatus.IN_PROGRESS)):
        db.session.add(CodeDevTask(
            project_id=project.id, session_id=session.id, feature_id=fid,
            title=f"已有 {fid}", status=status,
        ))
    db.session.commit()
    row = _draft_plan(project, session, [
        {"feature_id": "FR1.T1", "title": "企图覆盖 done"},
        {"feature_id": "FR1.T2", "title": "企图覆盖 active"},
        {"feature_id": "FR1.T3", "title": "新任务"},
    ])
    counts = planner.apply_plan(row, project, session)
    assert counts == {"inserted": 1, "updated": 0, "skipped": 2}
    db.session.expire_all()
    assert db.session.get(
        CodeDevTask,
        next(t.id for t in svc.session_tasks(session.id) if t.feature_id == "FR1.T1"),
    ).title == "已有 FR1.T1"


def test_apply_replace_refused_under_active_sprint(app):
    project = _project(str(uuid.uuid4()))
    session = _session(project)
    db.session.add(CodeDevSprint(
        project_id=project.id, session_id=session.id, lane="frontend",
        status=DevSprintStatus.RUNNING, created_by=project.user_id,
    ))
    db.session.commit()
    row = _draft_plan(project, session, [{"feature_id": "N.T1", "title": "新"}])
    with pytest.raises(svc.BulkWriteRefused):
        planner.apply_plan(row, project, session, replace=True)
    db.session.expire_all()
    # Refused apply returns the plan to draft (still applicable without replace).
    assert row.status == DevTaskPlanStatus.DRAFT


def test_apply_requires_draft_or_stale(app):
    project = _project(str(uuid.uuid4()))
    session = _session(project)
    row = _plan_row(session, status=DevTaskPlanStatus.APPLIED)
    with pytest.raises(planner.PlanNotApplicable):
        planner.apply_plan(row, project, session)


# --- workflow end-to-end (model faked) -----------------------------------------------
def _run_planner_workflow(app, run, monkeypatch, fanout_by_fr=None, invalid_text=None):
    """Drive the planner workflow with a faked FAST provider (fan-out path).

    ``fanout_by_fr``: rid -> per-FR JSON response (fine-grained split). Missing rids
    fail (→ coarse fallback for that FR). ``invalid_text``: return this unparseable
    text for every FR (→ all coarse → deterministic fallback)."""
    from backend.services.agent.recorder import RunRecorder
    from backend.services.agent.schemas import AgentContext
    from backend.services.agent.workflows.code_dev_backlog_planner_workflow import (
        run_code_dev_backlog_planner_workflow,
    )

    if invalid_text is not None:
        fake = _FakeProvider({"FR-01": invalid_text, "FR-02": invalid_text})
    else:
        fake = _FakeProvider(fanout_by_fr or {})
    import backend.services.ai.factory as fac
    monkeypatch.setattr(fac, "get_text_provider", lambda **k: fake)
    ctx = AgentContext(
        run_id=run.id, user_id=run.user_id, team_id=None, domain="code",
        workflow="code_dev_backlog_planner", resource_type="code_project",
        resource_id=run.resource_id, config=run.get_config(),
        input_snapshot={}, is_cancelled=lambda: False,
    )
    return run_code_dev_backlog_planner_workflow(ctx, RunRecorder(run.id))


def _planner_run(session, plan):
    from backend.models.agent import AgentRun, AgentRunStatus

    run = AgentRun(
        user_id=session.user_id, domain="code", workflow="code_dev_backlog_planner",
        resource_type="code_project", resource_id=session.project_id,
        status=AgentRunStatus.RUNNING,
    )
    run.set_config({"session_id": session.id, "plan_id": plan.id, "instruction": "拆核心路径"})
    db.session.add(run)
    db.session.commit()
    return run


def test_planner_workflow_produces_draft(app, monkeypatch):
    project = _project(str(uuid.uuid4()))
    session = _session(project)
    plan = _plan_row(session)
    run = _planner_run(session, plan)
    # Fan-out: each ledger FR is split into sub-tasks by the (faked) fast model.
    result = _run_planner_workflow(app, run, monkeypatch, fanout_by_fr={
        "FR-01": '{"tasks":[{"feature_id":"FR-01.T1","title":"订单列表","acceptance_criteria":["能看到列表"]},'
                 '{"feature_id":"FR-01.T2","title":"分页","acceptance_criteria":["能翻页"]}]}',
        "FR-02": '{"tasks":[{"feature_id":"FR-02.T1","title":"订单详情","acceptance_criteria":["能看详情"]}]}',
    })
    assert result["plan_id"] == plan.id
    db.session.expire_all()
    plan = db.session.get(CodeDevTaskPlan, plan.id)
    assert plan.status == DevTaskPlanStatus.DRAFT
    assert plan.input_fingerprint
    ids = sorted(t["feature_id"] for t in plan.get_plan()["tasks"])
    assert ids == ["FR-01.T1", "FR-01.T2", "FR-02.T1"]  # fine-grained sub-tasks
    assert "degraded:fallback" not in plan.get_warnings()
    assert plan.get_plan()["request"]["instruction"] == "拆核心路径"


def test_planner_workflow_falls_back_when_model_dead(app, monkeypatch):
    project = _project(str(uuid.uuid4()))
    session = _session(project)
    plan = _plan_row(session)
    run = _planner_run(session, plan)
    # No per-FR responses configured → every FR split fails → coarse fallback.
    result = _run_planner_workflow(app, run, monkeypatch, fanout_by_fr={})
    assert result["plan_id"] == plan.id
    db.session.expire_all()
    plan = db.session.get(CodeDevTaskPlan, plan.id)
    assert plan.status == DevTaskPlanStatus.DRAFT
    assert "degraded:fallback" in plan.get_warnings()
    # Fallback derives one coarse task per ledger FR.
    assert len(plan.get_plan()["tasks"]) == 2


def test_planner_workflow_invalid_json_falls_back(app, monkeypatch):
    project = _project(str(uuid.uuid4()))
    session = _session(project)
    plan = _plan_row(session)
    run = _planner_run(session, plan)
    _run_planner_workflow(app, run, monkeypatch, invalid_text="这不是 JSON")
    db.session.expire_all()
    plan = db.session.get(CodeDevTaskPlan, plan.id)
    assert plan.status == DevTaskPlanStatus.DRAFT
    assert "degraded:fallback" in plan.get_warnings()


# --- HTTP surface -----------------------------------------------------------------
def _client_and_token(app, user_id):
    from flask_jwt_extended import create_access_token

    return app.test_client(), create_access_token(identity=user_id)


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_task_plan_api_lifecycle(app, monkeypatch):
    from backend.services.agent.runtime import agent_runtime

    started: list = []
    monkeypatch.setattr(agent_runtime, "start", lambda app_, run_id: started.append(run_id))

    uid = str(uuid.uuid4())
    project = _project(uid)
    session = _session(project)
    client, token = _client_and_token(app, uid)
    base = f"/api/code/projects/{project.id}/dev-sessions/{session.id}/task-plans"

    resp = client.post(base, json={"instruction": "拆核心路径", "max_tasks": 10}, headers=_auth(token))
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    plan_id = data["plan"]["id"]
    assert data["plan"]["status"] == DevTaskPlanStatus.PLANNING
    assert started == [data["run_id"]]

    # Only one in-flight plan generation at a time.
    resp = client.post(base, json={}, headers=_auth(token))
    assert resp.status_code == 400

    # Simulate the workflow having produced a draft. The fingerprint must be
    # computed with the SAME request params recorded in plan.request (that is
    # exactly how the workflow pins it), else the lazy stale check trips.
    plan = db.session.get(CodeDevTaskPlan, plan_id)
    context = planner.build_planner_context(
        project, session, instruction="拆核心路径", max_tasks=10,
    )
    normalized, _ = planner.normalize_plan(
        {"tasks": [{"feature_id": "FR1.T1", "title": "订单列表"}]}, max_tasks=10,
    )
    normalized["request"] = {"instruction": "拆核心路径", "include_assets": True, "max_tasks": 10}
    plan.set_plan(normalized)
    plan.input_fingerprint = planner.input_fingerprint(context)
    plan.status = DevTaskPlanStatus.DRAFT
    db.session.commit()

    # GET one (with plan body) + list (without).
    resp = client.get(f"{base}/{plan_id}", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.get_json()["data"]["plan"]["plan"]["tasks"][0]["feature_id"] == "FR1.T1"
    resp = client.get(base, headers=_auth(token))
    assert resp.get_json()["data"]["plans"][0]["id"] == plan_id

    # PATCH edits + renormalizes.
    resp = client.patch(f"{base}/{plan_id}", json={
        "tasks": [
            {"feature_id": "FR1.T1", "title": "订单列表(改)", "depends_on": ["FR1.T1"]},
        ],
        "summary": "编辑后的摘要",
    }, headers=_auth(token))
    assert resp.status_code == 200
    body = resp.get_json()["data"]["plan"]
    assert body["plan"]["tasks"][0]["title"] == "订单列表(改)"
    assert body["plan"]["tasks"][0]["depends_on"] == []  # self-dep stripped

    # Apply → board.
    resp = client.post(f"{base}/{plan_id}/apply", json={}, headers=_auth(token))
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["inserted"] == 1
    assert any(t["feature_id"] == "FR1.T1" for t in data["board"]["items"])

    # Reject another draft.
    plan2 = _plan_row(session, status=DevTaskPlanStatus.DRAFT)
    resp = client.post(f"{base}/{plan2.id}/reject", json={}, headers=_auth(token))
    assert resp.get_json()["data"]["plan"]["status"] == DevTaskPlanStatus.REJECTED


def test_task_plan_api_stale_apply_conflict(app, monkeypatch):
    uid = str(uuid.uuid4())
    project = _project(uid)
    session = _session(project)
    row = _draft_plan(project, session, [{"feature_id": "S.T1", "title": "任务"}])
    # Drift after generation.
    db.session.add(CodeDevTask(
        project_id=project.id, session_id=session.id, feature_id="DRIFT.T1",
        title="漂移", status=DevTaskStatus.PENDING,
    ))
    db.session.commit()
    client, token = _client_and_token(app, uid)
    base = f"/api/code/projects/{project.id}/dev-sessions/{session.id}/task-plans"
    resp = client.post(f"{base}/{row.id}/apply", json={}, headers=_auth(token))
    assert resp.status_code == 409
    # GET lazily surfaces the staleness.
    resp = client.get(f"{base}/{row.id}", headers=_auth(token))
    assert resp.get_json()["data"]["plan"]["status"] == DevTaskPlanStatus.STALE
    # force applies.
    resp = client.post(f"{base}/{row.id}/apply", json={"force": True}, headers=_auth(token))
    assert resp.status_code == 200


def test_task_plan_api_owner_only(app):
    uid = str(uuid.uuid4())
    project = _project(uid)
    session = _session(project)
    stranger_client, stranger_token = _client_and_token(app, str(uuid.uuid4()))
    base = f"/api/code/projects/{project.id}/dev-sessions/{session.id}/task-plans"
    resp = stranger_client.post(base, json={}, headers=_auth(stranger_token))
    assert resp.status_code == 404


# --- fan-out per-FR decomposition (fake provider) -----------------------------
class _FakeResult:
    def __init__(self, text, success=True):
        self.text = text
        self.success = success
        self.error = None if success else "fail"


class _FakeProvider:
    """Fake fast provider: routes each per-FR prompt to a canned response by rid."""

    def __init__(self, by_fr):
        self.by_fr = by_fr
        self.timeout = 60.0
        self.max_retries = 0
        self._client = None

    def _configure(self):
        pass

    def generate_text(self, prompt):
        for rid, resp in self.by_fr.items():
            if f"({rid})" in prompt:  # "把下面这一个需求(FR-01)拆成…"
                if resp is None:
                    return _FakeResult("", success=False)
                return _FakeResult(resp)
        return _FakeResult("", success=False)


def _patch_fast_provider(monkeypatch, fake):
    import backend.services.ai.factory as fac
    monkeypatch.setattr(fac, "get_text_provider", lambda **k: fake)


def test_fr_split_prompt_forbids_unverifiable_ac():
    """The per-FR split prompt must steer the model away from AC that a single
    frontend turn can't produce / the reviewer can't judge from source (E2E tests,
    build-artifact inspection, whole-site sweeps) — those never close."""
    p = planner._fr_split_prompt("NFR3", "禁止前端收集用户私钥", "", 4)
    assert "自动化测试" in p and ("E2E" in p or "单元" in p)
    assert "打包" in p or "构建产物" in p
    assert "遍历所有页面" in p
    # still routes to the fake provider (keyed by "(NFR3)") — contract intact
    assert "(NFR3)" in p and "acceptance_criteria" in p


def test_fanout_decompose_produces_subtasks(app, monkeypatch):
    project = _project(str(uuid.uuid4()))
    session = _session(project)  # ledger: FR-01, FR-02
    _patch_fast_provider(monkeypatch, _FakeProvider({
        "FR-01": '{"tasks":[{"feature_id":"FR-01.T1","title":"列表视图","acceptance_criteria":["a","b"],"depends_on":[]},'
                 '{"feature_id":"FR-01.T2","title":"分页","acceptance_criteria":["c"],"depends_on":["FR-01.T1"]}]}',
        "FR-02": '{"tasks":[{"feature_id":"FR-02.T1","title":"详情页","acceptance_criteria":["d","e"]}]}',
    }))
    ctx = planner.build_planner_context(project, session)
    tasks, stats = planner.fanout_decompose(ctx)
    assert stats["ok"] == 2 and stats["fallback"] == 0
    assert sorted(t["feature_id"] for t in tasks) == ["FR-01.T1", "FR-01.T2", "FR-02.T1"]
    t2 = next(t for t in tasks if t["feature_id"] == "FR-01.T2")
    assert t2["parent_feature_id"] == "FR-01" and t2["depends_on"] == ["FR-01.T1"]


def test_fanout_per_fr_failure_falls_to_coarse(app, monkeypatch):
    project = _project(str(uuid.uuid4()))
    session = _session(project)
    _patch_fast_provider(monkeypatch, _FakeProvider({
        "FR-01": '{"tasks":[{"feature_id":"FR-01.T1","title":"ok","acceptance_criteria":["a"]}]}',
        "FR-02": None,  # this FR's model call fails → coarse fallback
    }))
    ctx = planner.build_planner_context(project, session)
    tasks, stats = planner.fanout_decompose(ctx)
    assert stats["ok"] == 1 and stats["fallback"] == 1
    fr02 = [t for t in tasks if t["parent_feature_id"] == "FR-02"]
    assert len(fr02) == 1 and fr02[0]["feature_id"] == "FR-02.T1"  # coarse


def test_fanout_forces_ids_into_fr_namespace(app, monkeypatch):
    """A model that echoes a wrong feature_id gets it forced back into the FR."""
    project = _project(str(uuid.uuid4()))
    session = _session(project)
    _patch_fast_provider(monkeypatch, _FakeProvider({
        "FR-01": '{"tasks":[{"feature_id":"WRONG.X","title":"t","acceptance_criteria":["a"]}]}',
        "FR-02": '{"tasks":[{"feature_id":"FR-02.T1","title":"t2","acceptance_criteria":["b"]}]}',
    }))
    ctx = planner.build_planner_context(project, session)
    tasks, _ = planner.fanout_decompose(ctx)
    fr01 = [t for t in tasks if t["parent_feature_id"] == "FR-01"]
    assert fr01[0]["feature_id"] == "FR-01.T1"  # WRONG.X forced back


def test_fanout_skips_done_features(app, monkeypatch):
    project = _project(str(uuid.uuid4()))
    session = _session(project)
    db.session.add(CodeDevTask(
        project_id=project.id, session_id=session.id, feature_id="FR-01",
        title="首页", status=DevTaskStatus.DONE,
    ))
    db.session.commit()
    _patch_fast_provider(monkeypatch, _FakeProvider({
        "FR-02": '{"tasks":[{"feature_id":"FR-02.T1","title":"t","acceptance_criteria":["a"]}]}',
    }))
    ctx = planner.build_planner_context(project, session)
    tasks, stats = planner.fanout_decompose(ctx)
    assert stats["total"] == 1  # only FR-02 (FR-01 done)
    assert all(t["parent_feature_id"] == "FR-02" for t in tasks)


def test_build_raw_plan_fanout_mode(app, monkeypatch):
    project = _project(str(uuid.uuid4()))
    session = _session(project)
    _patch_fast_provider(monkeypatch, _FakeProvider({
        "FR-01": '{"tasks":[{"feature_id":"FR-01.T1","title":"a","acceptance_criteria":["x"]}]}',
        "FR-02": '{"tasks":[{"feature_id":"FR-02.T1","title":"b","acceptance_criteria":["y"]}]}',
    }))
    ctx = planner.build_planner_context(project, session)
    raw, mode = planner.build_raw_plan(ctx)
    assert mode == "fanout"
    plan, _ = planner.normalize_plan(raw, max_tasks=ctx["max_tasks"])
    assert sorted(t["feature_id"] for t in plan["tasks"]) == ["FR-01.T1", "FR-02.T1"]


def test_build_raw_plan_all_fail_is_fallback(app, monkeypatch):
    project = _project(str(uuid.uuid4()))
    session = _session(project)
    _patch_fast_provider(monkeypatch, _FakeProvider({"FR-01": None, "FR-02": None}))
    ctx = planner.build_planner_context(project, session)
    raw, mode = planner.build_raw_plan(ctx)
    assert mode == "fallback"  # every FR fell to coarse
    assert "degraded:fallback" in raw["warnings"]
    assert len(raw["tasks"]) == 2  # coarse FR-01.T1, FR-02.T1


# --- auto-decompose coarse ledger-seed tasks before a sprint ------------------
def _coarse_seed(session, project, *, fid="FR1", title=None, parent=None,
                 source=DevTaskSource.LEDGER_SEED, criteria=None,
                 status=DevTaskStatus.PENDING, retry_count=0) -> CodeDevTask:
    """A board task mirroring seed_checklist output: one whole FR as the title, no
    granular acceptance criteria (the structurally-unwinnable case)."""
    t = CodeDevTask(
        project_id=project.id, session_id=session.id, feature_id=fid,
        parent_feature_id=parent, status=status, source=source, retry_count=retry_count,
        title=title or "首页框架:安全警示条、MegaMenu、Hero、价值卖点、用户分流、信任背书、页脚等",
    )
    if criteria:
        t.set_acceptance_criteria(criteria)
    db.session.add(t)
    db.session.commit()
    return t


def test_decompose_coarse_seed_splits_and_retires_parent(app, monkeypatch):
    project = _project(str(uuid.uuid4()))
    session = _session(project)
    parent = _coarse_seed(session, project, fid="FR1")
    _patch_fast_provider(monkeypatch, _FakeProvider({
        "FR1": '{"tasks":[{"feature_id":"FR1.T1","title":"安全警示条","acceptance_criteria":["顶部安全警示条可见"]},'
               '{"feature_id":"FR1.T2","title":"MegaMenu","acceptance_criteria":["悬停展开二级菜单"]}]}',
    }))
    out = planner.decompose_coarse_seed_tasks(project, session)
    assert out == {"candidates": 1, "decomposed": 1, "sub_tasks": 2, "unsplit": 0}
    db.session.refresh(parent)
    assert parent.status == DevTaskStatus.SKIPPED  # retired, not schedulable, not blocking
    children = [t for t in svc.session_tasks(session.id) if t.parent_feature_id == "FR1"]
    assert sorted(c.feature_id for c in children) == ["FR1.T1", "FR1.T2"]
    assert all(c.status == DevTaskStatus.PENDING for c in children)
    assert all(c.get_acceptance_criteria() for c in children)  # concrete, winnable AC
    assert all(c.source == DevTaskSource.PLANNER for c in children)


def test_decompose_recovers_blocked_monolith(app, monkeypatch):
    """A coarse seed that already exhausted retries (BLOCKED) is self-healed on the
    next sprint: split into winnable children + retired (blocked_reason cleared)."""
    project = _project(str(uuid.uuid4()))
    session = _session(project)
    parent = _coarse_seed(session, project, fid="FR1",
                          status=DevTaskStatus.BLOCKED, retry_count=2)
    parent.blocked_reason = "重试 2 次仍未通过:[FR1] 首页框架子区域未见完整"
    db.session.commit()
    _patch_fast_provider(monkeypatch, _FakeProvider({
        "FR1": '{"tasks":[{"feature_id":"FR1.T1","title":"安全警示条","acceptance_criteria":["可见"]}]}',
    }))
    out = planner.decompose_coarse_seed_tasks(project, session)
    assert out["candidates"] == 1 and out["decomposed"] == 1
    db.session.refresh(parent)
    assert parent.status == DevTaskStatus.SKIPPED
    assert parent.blocked_reason is None  # cleared on retire
    children = [t for t in svc.session_tasks(session.id) if t.parent_feature_id == "FR1"]
    assert [c.feature_id for c in children] == ["FR1.T1"]
    assert all(c.status == DevTaskStatus.PENDING for c in children)


def test_decompose_leaves_unsplittable_parent_pending(app, monkeypatch):
    project = _project(str(uuid.uuid4()))
    session = _session(project)
    parent = _coarse_seed(session, project, fid="FR1")
    _patch_fast_provider(monkeypatch, _FakeProvider({"FR1": None}))  # model call fails
    out = planner.decompose_coarse_seed_tasks(project, session)
    assert out["candidates"] == 1 and out["decomposed"] == 0 and out["unsplit"] == 1
    db.session.refresh(parent)
    assert parent.status == DevTaskStatus.PENDING  # untouched — runs coarse as before


def test_decompose_no_text_lane_leaves_board_untouched(app, monkeypatch):
    project = _project(str(uuid.uuid4()))
    session = _session(project)
    parent = _coarse_seed(session, project, fid="FR1")
    import backend.services.ai.factory as fac
    monkeypatch.setattr(fac, "get_text_provider", lambda **k: None)  # unconfigured
    out = planner.decompose_coarse_seed_tasks(project, session)
    assert out["candidates"] == 1 and out["decomposed"] == 0 and out["unsplit"] == 1
    db.session.refresh(parent)
    assert parent.status == DevTaskStatus.PENDING


def test_decompose_idempotent_when_children_exist(app, monkeypatch):
    project = _project(str(uuid.uuid4()))
    session = _session(project)
    _coarse_seed(session, project, fid="FR1")
    _coarse_seed(session, project, fid="FR1.T1", title="子任务", parent="FR1",
                 source=DevTaskSource.PLANNER, criteria=["a"])
    _patch_fast_provider(monkeypatch, _FakeProvider({
        "FR1": '{"tasks":[{"feature_id":"FR1.T2","title":"x","acceptance_criteria":["b"]}]}',
    }))
    out = planner.decompose_coarse_seed_tasks(project, session)
    assert out["candidates"] == 0  # FR1 already has a child → not re-split


def test_decompose_skips_tasks_that_already_have_ac(app, monkeypatch):
    project = _project(str(uuid.uuid4()))
    session = _session(project)
    _coarse_seed(session, project, fid="FR1", criteria=["已有具体、可判定的验收标准"])
    _patch_fast_provider(monkeypatch, _FakeProvider({
        "FR1": '{"tasks":[{"feature_id":"FR1.T1","title":"x","acceptance_criteria":["a"]}]}',
    }))
    out = planner.decompose_coarse_seed_tasks(project, session)
    assert out["candidates"] == 0  # already winnable-shaped → leave it


def test_decompose_disabled_by_env(app, monkeypatch):
    project = _project(str(uuid.uuid4()))
    session = _session(project)
    parent = _coarse_seed(session, project, fid="FR1")
    monkeypatch.setattr(planner, "_AUTO_DECOMPOSE_ENABLED", False)
    out = planner.decompose_coarse_seed_tasks(project, session)
    assert out == {"candidates": 0, "decomposed": 0, "sub_tasks": 0, "unsplit": 0}
    db.session.refresh(parent)
    assert parent.status == DevTaskStatus.PENDING


def test_retire_superseded_fires_from_pending_or_blocked_only(app):
    project = _project(str(uuid.uuid4()))
    session = _session(project)
    t = _coarse_seed(session, project, fid="FR1")
    assert svc.retire_superseded(t.id, "FR1.T1, FR1.T2") is True
    db.session.refresh(t)
    assert t.status == DevTaskStatus.SKIPPED
    assert "FR1.T1" in (t.note or "")
    assert svc.retire_superseded(t.id, "x") is False  # already skipped → no-op

    blocked = _coarse_seed(session, project, fid="FR2", status=DevTaskStatus.BLOCKED)
    assert svc.retire_superseded(blocked.id, "FR2.T1") is True  # blocked → skipped
    db.session.refresh(blocked)
    assert blocked.status == DevTaskStatus.SKIPPED

    active = _coarse_seed(session, project, fid="FR3", status=DevTaskStatus.IN_PROGRESS)
    assert svc.retire_superseded(active.id, "x") is False  # a turn owns it → untouched
    db.session.refresh(active)
    assert active.status == DevTaskStatus.IN_PROGRESS
