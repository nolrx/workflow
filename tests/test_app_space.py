"""
Tests for App Space (应用空间) + secondary development (二次开发).

Network-free: the text provider is forced unavailable so the impact analysis
takes its deterministic fallback and contract synthesis takes its fallback path,
and ``agent_runtime.start`` is stubbed so creating a run never spawns a worker.
Covers:
  * app list permission (owner-only) + deploy-status filter + no cross-user leak
  * app detail ownership (404 for another user's app)
  * iteration creation (requires a deployment, requires an instruction)
  * impact-scope parsing (ImpactScope + the deterministic analyzer)
  * confirm starts exactly the lanes the scope maps to
  * a deploying app cannot be deleted out from under its container
"""
import pytest
from flask_jwt_extended import create_access_token

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


@pytest.fixture(autouse=True)
def _no_text_provider(monkeypatch):
    """Force the deterministic (no-AI) path everywhere in these tests."""
    monkeypatch.setattr("backend.services.ai.get_text_provider", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _no_runtime_start(monkeypatch):
    """Never actually dispatch a worker when a route starts a run."""
    from backend.services.agent import runtime as rt

    monkeypatch.setattr(rt.agent_runtime, "start", lambda app, run_id: None)


_FLOW = """# 开发流程

## 技术假设
后端采用 Node.js + Express,数据库用 PostgreSQL。

## 数据设计
Task 实体:id、title、done。

## 接口设计
GET /tasks 返回任务列表;POST /tasks 创建任务。

## 后端服务
单一 REST 服务。

## AI/提示词链路
不涉及 AI。
"""


def _make_project(user_id="u1", title="待办应用"):
    from backend.models.code import CodeProject, CodeProjectStatus

    project = CodeProject(
        user_id=user_id,
        title=title,
        requirement_input="做一个待办应用",
        requirements_doc="# 需求\nFR1 可创建/勾选任务。",
        development_flow=_FLOW,
        status=CodeProjectStatus.UI_CONFIRMED,
    )
    db.session.add(project)
    db.session.commit()
    return project


def _make_deployment(project, status="running", health="healthy"):
    from backend.models.code.fullstack import CodeDeployment

    dep = CodeDeployment(
        project_id=project.id,
        user_id=project.user_id,
        status=status,
        health=health,
        api_base_path=f"/app/{project.id}/api",
    )
    db.session.add(dep)
    db.session.commit()
    return dep


def _auth(user_id):
    return {"Authorization": f"Bearer {create_access_token(identity=user_id)}"}


# --- impact-scope parsing (pure) ---------------------------------------------
def test_impact_scope_lane_mapping():
    from backend.models.code.fullstack import ImpactScope

    assert ImpactScope.lanes_for("frontend") == ["frontend"]
    assert ImpactScope.lanes_for("backend") == ["backend"]
    assert ImpactScope.lanes_for("backend_middleware") == ["backend", "middleware"]
    assert ImpactScope.lanes_for("fullstack") == ["frontend", "backend", "middleware"]
    # Unknown / None defaults to the safest non-empty lane.
    assert ImpactScope.lanes_for(None) == ["backend"]
    assert ImpactScope.lanes_for("bogus") == ["backend"]


def test_impact_scope_from_lanes():
    from backend.models.code.fullstack import ImpactScope

    assert ImpactScope.from_lanes(["frontend"]) == "frontend"
    assert ImpactScope.from_lanes(["frontend", "backend"]) == "frontend_backend"
    assert ImpactScope.from_lanes(["backend", "middleware"]) == "backend_middleware"
    assert ImpactScope.from_lanes(["frontend", "backend", "middleware"]) == "fullstack"
    assert ImpactScope.from_lanes([]) == "backend"


def test_deterministic_analysis_scope_rules():
    from backend.services.agent.workflows.code_app_iteration_workflow import (
        _deterministic_analysis,
    )

    # Pure UI tweak → frontend only, low risk.
    ui = _deterministic_analysis("把首页换成更科技感的配色和文案", "ui_change")
    assert ui["recommended_lanes"] == ["frontend"]
    assert ui["risk_level"] == "low"
    assert ui["requires_user_confirmation"] is False

    # New feature touching data → full stack + database change + high risk.
    feat = _deterministic_analysis("新增会员等级与权益，需要新增数据表", "new_feature")
    assert "middleware" in feat["recommended_lanes"]
    assert feat["database_change"] is True
    assert feat["risk_level"] == "high"
    assert feat["requires_user_confirmation"] is True

    # Auth/permission surface always forces confirmation regardless of type.
    login = _deterministic_analysis("调整登录与权限校验逻辑", "backend_logic")
    assert login["risk_level"] == "high"
    assert login["requires_user_confirmation"] is True


# --- app list permission + filters -------------------------------------------
def test_list_apps_owner_only_and_status_filter(app):
    p_run = _make_project("u1", "运行中应用")
    _make_deployment(p_run, status="running")
    p_fail = _make_project("u1", "失败应用")
    _make_deployment(p_fail, status="failed", health="unhealthy")
    # Another user's deployed app must never appear in u1's list.
    p_other = _make_project("u2", "他人应用")
    _make_deployment(p_other, status="running")

    client = app.test_client()
    res = client.get("/api/code/apps", headers=_auth("u1"))
    assert res.status_code == 200
    data = res.get_json()["data"]
    titles = {a["title"] for a in data["apps"]}
    assert titles == {"运行中应用", "失败应用"}
    assert data["total"] == 2
    assert "他人应用" not in titles

    # Status filter narrows to the running one.
    res2 = client.get("/api/code/apps?status=running", headers=_auth("u1"))
    apps2 = res2.get_json()["data"]["apps"]
    assert len(apps2) == 1 and apps2[0]["title"] == "运行中应用"
    assert apps2[0]["is_running"] is True


def test_get_app_detail_owner_only(app):
    project = _make_project("u1")
    _make_deployment(project)

    client = app.test_client()
    # Owner sees the detail.
    ok = client.get(f"/api/code/apps/{project.id}", headers=_auth("u1"))
    assert ok.status_code == 200
    body = ok.get_json()["data"]
    assert body["project"]["id"] == project.id
    assert body["api_base_path"] == f"/app/{project.id}/api"
    assert body["preview_url"] == f"/preview/{project.id}/"

    # Another user gets 404 (no existence leak).
    denied = client.get(f"/api/code/apps/{project.id}", headers=_auth("u2"))
    assert denied.status_code == 404


# --- iteration creation ------------------------------------------------------
def test_create_iteration_requires_deployment(app):
    project = _make_project("u1")  # no deployment
    client = app.test_client()
    res = client.post(
        f"/api/code/apps/{project.id}/iterations",
        headers=_auth("u1"),
        json={"instruction": "随便改点东西"},
    )
    assert res.status_code == 400
    assert res.get_json()["error"] == "VALIDATION_ERROR"


def test_create_iteration_requires_instruction(app):
    project = _make_project("u1")
    _make_deployment(project)
    client = app.test_client()
    res = client.post(
        f"/api/code/apps/{project.id}/iterations",
        headers=_auth("u1"),
        json={"instruction": "   "},
    )
    assert res.status_code == 400


def test_create_iteration_starts_analysis(app):
    from backend.models.agent import AgentRun
    from backend.models.code.fullstack import CodeAppIteration, IterationStatus

    project = _make_project("u1")
    _make_deployment(project)
    client = app.test_client()
    res = client.post(
        f"/api/code/apps/{project.id}/iterations",
        headers=_auth("u1"),
        json={"instruction": "新增会员中心页面", "change_type": "new_feature"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    it = res.get_json()["data"]["iteration"]
    assert it["status"] == IterationStatus.ANALYZING
    assert it["analysis_run_id"]

    row = db.session.get(CodeAppIteration, it["id"])
    assert row.user_id == "u1"
    run = db.session.get(AgentRun, row.analysis_run_id)
    assert run.workflow == "code_app_iteration_analysis"
    assert run.resource_id == project.id


def test_create_iteration_forbidden_for_non_owner(app):
    project = _make_project("u1")
    _make_deployment(project)
    client = app.test_client()
    res = client.post(
        f"/api/code/apps/{project.id}/iterations",
        headers=_auth("u2"),
        json={"instruction": "改点东西"},
    )
    assert res.status_code == 404


# --- confirm starts the right lanes ------------------------------------------
def _seed_awaiting_iteration(project, lanes, scope):
    from backend.models.code.fullstack import CodeAppIteration, IterationStatus

    it = CodeAppIteration(
        project_id=project.id,
        user_id=project.user_id,
        instruction="x",
        change_type="new_feature",
        impact_scope=scope,
        status=IterationStatus.AWAITING_PLAN_APPROVAL,
    )
    it.set_analysis({"recommended_lanes": lanes, "contract_change": False, "database_change": False})
    it.set_plan({"scope": scope, "steps": []})
    db.session.add(it)
    db.session.commit()
    return it


def test_confirm_backend_only_starts_one_lane(app):
    from backend.models.agent import AgentRun
    from backend.models.code.fullstack import CodeAppIteration, IterationStatus

    project = _make_project("u1")
    _make_deployment(project)
    it = _seed_awaiting_iteration(project, ["backend"], "backend")

    client = app.test_client()
    res = client.post(
        f"/api/code/apps/{project.id}/iterations/{it.id}/confirm",
        headers=_auth("u1"),
        json={},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    runs = res.get_json()["data"]["runs"]
    assert set(runs) == {"backend"}

    row = db.session.get(CodeAppIteration, it.id)
    assert row.status == IterationStatus.GENERATING
    assert row.backend_run_id and not row.frontend_run_id
    started = AgentRun.query.filter_by(
        resource_id=project.id, workflow="code_backend_project_generation"
    ).count()
    assert started == 1


def test_confirm_fullstack_starts_three_lanes(app):
    project = _make_project("u1")
    _make_deployment(project)
    it = _seed_awaiting_iteration(
        project, ["frontend", "backend", "middleware"], "fullstack"
    )

    client = app.test_client()
    res = client.post(
        f"/api/code/apps/{project.id}/iterations/{it.id}/confirm",
        headers=_auth("u1"),
        json={},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    runs = res.get_json()["data"]["runs"]
    assert set(runs) == {"frontend", "backend", "middleware"}


def test_confirm_rejected_when_not_awaiting(app):
    from backend.models.code.fullstack import CodeAppIteration, IterationStatus

    project = _make_project("u1")
    _make_deployment(project)
    it = CodeAppIteration(
        project_id=project.id,
        user_id="u1",
        instruction="x",
        change_type="other",
        status=IterationStatus.ANALYZING,  # not yet awaiting approval
    )
    db.session.add(it)
    db.session.commit()

    client = app.test_client()
    res = client.post(
        f"/api/code/apps/{project.id}/iterations/{it.id}/confirm",
        headers=_auth("u1"),
        json={},
    )
    assert res.status_code == 409


# --- deploying app cannot be deleted -----------------------------------------
def test_deploying_app_cannot_be_deleted(app):
    """A project with an ACTIVE deployment must not be deletable (would orphan its
    container / db namespace) — the App Space relies on this guard for 停止部署 P1."""
    project = _make_project("u1")
    _make_deployment(project, status="running")

    client = app.test_client()
    res = client.delete(f"/api/code/projects/{project.id}", headers=_auth("u1"))
    assert res.status_code == 409
    assert res.get_json()["error"] == "DEPLOYMENT_ACTIVE"


# --- analysis workflow end to end (deterministic) ----------------------------
def test_analysis_workflow_produces_plan_and_awaits_approval(app):
    """Running the analysis workflow directly drives the iteration to
    AWAITING_PLAN_APPROVAL with a concrete analysis + plan (deterministic path)."""
    from backend.models.agent import AgentRun, AgentRunStatus
    from backend.models.code.fullstack import CodeAppIteration, IterationStatus
    from backend.services.agent.recorder import RunRecorder
    from backend.services.agent.schemas import AgentContext
    from backend.services.agent.workflows.code_app_iteration_workflow import (
        run_code_app_iteration_analysis_workflow,
    )

    project = _make_project("u1")
    _make_deployment(project)
    it = CodeAppIteration(
        project_id=project.id,
        user_id="u1",
        instruction="新增会员等级与权益，需要新增数据库表",
        change_type="new_feature",
        status=IterationStatus.DRAFT,
    )
    db.session.add(it)
    db.session.commit()

    run = AgentRun(
        user_id="u1", domain="code", workflow="code_app_iteration_analysis",
        resource_type="code_project", resource_id=project.id,
        status=AgentRunStatus.RUNNING, credit_reserved=0,
    )
    run.set_config({"iteration_id": it.id})
    db.session.add(run)
    db.session.commit()

    ctx = AgentContext(
        run_id=run.id, user_id="u1", team_id=None, domain="code",
        workflow="code_app_iteration_analysis", resource_type="code_project",
        resource_id=project.id, config={"iteration_id": it.id},
    )
    result = run_code_app_iteration_analysis_workflow(ctx, RunRecorder(run.id))
    assert result["status"] == AgentRunStatus.COMPLETED

    db.session.expire_all()
    row = db.session.get(CodeAppIteration, it.id)
    assert row.status == IterationStatus.AWAITING_PLAN_APPROVAL
    assert row.impact_scope == "fullstack"
    analysis = row.get_analysis()
    assert analysis["database_change"] is True
    plan = row.get_plan()
    assert plan["steps"] and plan["steps"][-1]["lane"] == "deploy"


def test_analysis_workflow_refuses_other_users_iteration(app):
    """Direct invocation with a victim's ids (a forged POST /api/agent/runs) must
    leak/mutate nothing: the ownership gate raises before any status change."""
    from backend.models.agent import AgentRun, AgentRunStatus
    from backend.models.code.fullstack import CodeAppIteration, IterationStatus
    from backend.services.agent.recorder import RunRecorder
    from backend.services.agent.schemas import AgentContext
    from backend.services.agent.workflows.code_app_iteration_workflow import (
        run_code_app_iteration_analysis_workflow,
    )

    project = _make_project("victim")
    _make_deployment(project)
    it = CodeAppIteration(
        project_id=project.id, user_id="victim", instruction="victim's change",
        change_type="other", status=IterationStatus.DRAFT,
    )
    db.session.add(it)
    db.session.commit()

    # Attacker's run carries THEIR identity but references the victim's ids.
    run = AgentRun(
        user_id="attacker", domain="code", workflow="code_app_iteration_analysis",
        resource_type="code_project", resource_id=project.id,
        status=AgentRunStatus.RUNNING, credit_reserved=0,
    )
    run.set_config({"iteration_id": it.id})
    db.session.add(run)
    db.session.commit()

    ctx = AgentContext(
        run_id=run.id, user_id="attacker", team_id=None, domain="code",
        workflow="code_app_iteration_analysis", resource_type="code_project",
        resource_id=project.id, config={"iteration_id": it.id},
    )
    with pytest.raises(ValueError):
        run_code_app_iteration_analysis_workflow(ctx, RunRecorder(run.id))

    db.session.expire_all()
    # The victim's iteration is untouched (still DRAFT, no analysis written).
    row = db.session.get(CodeAppIteration, it.id)
    assert row.status == IterationStatus.DRAFT
    assert row.get_analysis() == {}


# --- node #3: true iteration (续改) plumbing ---------------------------------
class _Ctx:
    """Minimal stand-in for AgentContext (only the fields the helpers read)."""

    def __init__(self, config):
        self.config = config


def test_iteration_change_reads_instruction_and_plan(app):
    from backend.models.code.fullstack import CodeAppIteration, IterationStatus
    from backend.services.agent.workflows._iteration_support import iteration_change

    project = _make_project("u1")
    it = CodeAppIteration(
        project_id=project.id, user_id="u1", instruction="给任务接口加 done 过滤",
        change_type="backend_logic", status=IterationStatus.GENERATING,
    )
    it.set_plan({"steps": [{"lane": "backend", "action": "modify", "description": "加过滤参数"}]})
    db.session.add(it)
    db.session.commit()

    assert iteration_change(_Ctx({})) is None  # not an iteration run
    change = iteration_change(_Ctx({"iteration_id": it.id}))
    assert change and change["instruction"] == "给任务接口加 done 过滤"
    assert "backend" in change["plan_text"] and "加过滤参数" in change["plan_text"]


def test_load_prior_source_from_zip(app):
    import io
    import zipfile

    from backend.models.agent import AgentArtifact
    from backend.services.agent.files import save_artifact_file
    from backend.services.agent.workflows._iteration_support import load_prior_source

    project = _make_project("u1")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("package.json", '{"name":"x"}')
        z.writestr("src/main.ts", "console.log(1)")
    rel = save_artifact_file("run-x", "step-x", "frontend_project.zip", buf.getvalue())
    art = AgentArtifact(
        run_id="run-x", artifact_type="text", title="src",
        domain_ref_type="code_frontend_project_zip", domain_ref_id=project.id,
        storage_path=rel, filename="frontend_project.zip",
    )
    db.session.add(art)
    db.session.commit()

    files = load_prior_source(project.id, "frontend")
    assert set(files) == {"package.json", "src/main.ts"}
    assert files["src/main.ts"] == b"console.log(1)"
    # A lane with no prior source returns {} (workflow falls back to from-scratch).
    assert load_prior_source(project.id, "backend") == {}


def test_build_prompt_edit_mode_injects_change(app):
    from backend.services.code.backend_project_service import BackendProjectService

    svc = BackendProjectService()
    fill_vals = {"CONTEXT_LEDGER": "", "REQUIREMENT": "", "REQUIREMENTS_DOC": "",
                 "DEVELOPMENT_FLOW": "", "DOCUMENTS": "", "CONTRACT": "", "MIDDLEWARE": ""}
    edit = svc._build_prompt(fill_vals, True, {"src/a.py": b"x"}, "加一个 done 过滤参数", "plan-text")
    assert "加一个 done 过滤参数" in edit  # change instruction flows into the prompt
    assert "续改" in edit or "现有" in edit  # edit-mode framing
    fresh = svc._build_prompt(fill_vals, False, {}, "加一个 done 过滤参数", "")
    assert "加一个 done 过滤参数" not in fresh  # fresh generation never sees the change


def test_seed_base_writes_files(tmp_path):
    from backend.services.code.backend_project_service import BackendProjectService

    BackendProjectService._seed_base(tmp_path / "_base", {"a.txt": b"hello", "d/b.txt": b"world"})
    assert (tmp_path / "_base" / "a.txt").read_bytes() == b"hello"
    assert (tmp_path / "_base" / "d" / "b.txt").read_bytes() == b"world"


def test_seed_base_blocks_path_traversal(tmp_path):
    """A crafted zip member name must not escape base_dir onto the host."""
    from backend.services.code.backend_project_service import BackendProjectService
    from backend.services.code.frontend_project_service import FrontendProjectService

    base = tmp_path / "work" / "_base"
    evil = {
        "../../escape.txt": b"pwned",
        "/etc/escape.txt": b"pwned",
        "ok/file.txt": b"safe",
    }
    for svc in (BackendProjectService, FrontendProjectService):
        svc._seed_base(base, evil)
    # The safe file landed; neither traversal escaped to a sibling/root.
    assert (base / "ok" / "file.txt").read_bytes() == b"safe"
    assert not (tmp_path / "escape.txt").exists()
    assert not (tmp_path / "work" / "escape.txt").exists()


# --- resources / database / code entries -------------------------------------
def _make_source_artifact(project_id, domain_ref_type, files):
    import io
    import zipfile

    from backend.models.agent import AgentArtifact
    from backend.services.agent.files import save_artifact_file

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    rel = save_artifact_file("r-" + domain_ref_type, "s", "src.zip", buf.getvalue())
    art = AgentArtifact(
        run_id="r-" + domain_ref_type, artifact_type="text", title="src",
        domain_ref_type=domain_ref_type, domain_ref_id=project_id,
        storage_path=rel, filename="src.zip",
    )
    db.session.add(art)
    db.session.commit()
    return art


def test_resources_owner_only_and_shape(app):
    project = _make_project("u1")
    _make_deployment(project)
    _make_source_artifact(project.id, "code_frontend_project_zip", {"index.html": "<h1>", "src/app.tsx": "x"})

    client = app.test_client()
    res = client.get(f"/api/code/apps/{project.id}/resources", headers=_auth("u1"))
    assert res.status_code == 200
    data = res.get_json()["data"]
    assert data["frontend"]["file_count"] == 2
    assert data["frontend"]["download_url"].endswith("/file?download=1")
    assert data["database"]["engine"] == "sqlite"  # no db_name on the test deployment
    assert data["preview_url"] == f"/preview/{project.id}/"

    # Another user cannot read it.
    assert client.get(f"/api/code/apps/{project.id}/resources", headers=_auth("u2")).status_code == 404


def test_code_listing_and_file_view(app):
    project = _make_project("u1")
    _make_deployment(project)
    _make_source_artifact(
        project.id, "code_backend_project_zip",
        {"main.py": "print('hi')", "Dockerfile": "FROM python"},
    )
    client = app.test_client()

    listing = client.get(f"/api/code/apps/{project.id}/code?lane=backend", headers=_auth("u1"))
    assert listing.status_code == 200
    files = listing.get_json()["data"]["files"]
    assert files == ["Dockerfile", "main.py"]

    view = client.get(
        f"/api/code/apps/{project.id}/code/file?lane=backend&path=main.py", headers=_auth("u1")
    )
    assert view.status_code == 200
    body = view.get_json()["data"]
    assert body["content"] == "print('hi')"
    assert body["is_binary"] is False

    # Missing file → 404; unknown lane → 400.
    assert client.get(
        f"/api/code/apps/{project.id}/code/file?lane=backend&path=nope.py", headers=_auth("u1")
    ).status_code == 404
    assert client.get(
        f"/api/code/apps/{project.id}/code?lane=bogus", headers=_auth("u1")
    ).status_code == 400
    # Owner-only.
    assert client.get(
        f"/api/code/apps/{project.id}/code?lane=backend", headers=_auth("u2")
    ).status_code == 404


def _make_running_deployment(project):
    dep = _make_deployment(project, status="running", health="healthy")
    dep.container_name = "app-test"
    dep.internal_port = 8080
    db.session.commit()
    return dep


def test_stop_app_sets_stopped_and_owner_only(app, monkeypatch):
    from backend.models.code.fullstack import CodeDeployment, DeploymentStatus
    from backend.services.code import deploy_service

    removed = []
    monkeypatch.setattr(deploy_service, "_remove_container", lambda name: removed.append(name))

    project = _make_project("u1")
    _make_running_deployment(project)
    client = app.test_client()

    # Another user cannot stop it.
    assert client.post(f"/api/code/apps/{project.id}/stop", headers=_auth("u2")).status_code == 404

    res = client.post(f"/api/code/apps/{project.id}/stop", headers=_auth("u1"))
    assert res.status_code == 200
    assert res.get_json()["data"]["status"] == DeploymentStatus.STOPPED
    dep = CodeDeployment.query.filter_by(project_id=project.id).first()
    assert dep.status == DeploymentStatus.STOPPED
    assert removed == ["app-test"]  # container actually torn down


def test_app_logs_owner_only_and_shape(app, monkeypatch):
    import subprocess

    from backend.services.code import deploy_service

    project = _make_project("u1")
    _make_running_deployment(project)
    monkeypatch.setattr(
        deploy_service,
        "_docker",
        lambda args, timeout: subprocess.CompletedProcess(args, 0, stdout="2026 boot ok\n", stderr=""),
    )
    client = app.test_client()

    res = client.get(f"/api/code/apps/{project.id}/logs?tail=50", headers=_auth("u1"))
    assert res.status_code == 200
    data = res.get_json()["data"]
    assert data["available"] is True and "boot ok" in data["logs"]

    assert client.get(f"/api/code/apps/{project.id}/logs", headers=_auth("u2")).status_code == 404


def test_app_logs_no_container(app):
    project = _make_project("u1")
    _make_deployment(project, status="failed")  # no container_name
    client = app.test_client()
    res = client.get(f"/api/code/apps/{project.id}/logs", headers=_auth("u1"))
    assert res.status_code == 200
    assert res.get_json()["data"]["available"] is False


def test_health_refresh(app, monkeypatch):
    import requests

    from backend.models.code.fullstack import CodeDeployment
    from backend.services.code import deploy_service  # noqa: F401 — ensures module import

    project = _make_project("u1")
    _make_running_deployment(project)

    class _Resp:
        status_code = 200

    monkeypatch.setattr(requests, "get", lambda url, timeout=None: _Resp())
    client = app.test_client()
    res = client.post(f"/api/code/apps/{project.id}/health/refresh", headers=_auth("u1"))
    assert res.status_code == 200
    body = res.get_json()["data"]
    assert body["available"] is True and body["health"] == "healthy"
    assert CodeDeployment.query.filter_by(project_id=project.id).first().health == "healthy"

    # Owner-only.
    assert client.post(f"/api/code/apps/{project.id}/health/refresh", headers=_auth("u2")).status_code == 404


def test_health_refresh_not_running(app):
    project = _make_project("u1")
    _make_deployment(project, status="stopped")
    client = app.test_client()
    res = client.post(f"/api/code/apps/{project.id}/health/refresh", headers=_auth("u1"))
    assert res.status_code == 200
    assert res.get_json()["data"]["available"] is False


def test_apps_pagination(app):
    for i in range(3):
        p = _make_project("u1", f"app{i}")
        _make_deployment(p)
    client = app.test_client()

    page1 = client.get("/api/code/apps?limit=2&offset=0", headers=_auth("u1")).get_json()["data"]
    assert len(page1["apps"]) == 2 and page1["total"] == 3
    page2 = client.get("/api/code/apps?limit=2&offset=2", headers=_auth("u1")).get_json()["data"]
    assert len(page2["apps"]) == 1 and page2["total"] == 3


def test_mount_failure_hint_detects_dood_mount_error():
    """The cryptic OCI bind-mount error becomes an actionable hint (restart backend)."""
    from backend.services.code.docker_env import mount_failure_hint

    real = (
        "docker: Error response from daemon: failed to create task for container: "
        "failed to create shim task: OCI runtime create failed: runc create failed: "
        "unable to start container process: error during container init: failed to "
        "fulfil mount request: open /dev/root/data/workflow/.fe-agent-work/"
        "fe-agent-4fb9bovz: not a directory"
    )
    hint = mount_failure_hint(real)
    assert hint and "force-recreate" in hint and ".fe-agent-work" in hint
    # Unrelated stderr → no false-positive hint.
    assert mount_failure_hint("npm ERR! build failed: TypeError x is undefined") == ""
    assert mount_failure_hint("") == ""


def test_database_sqlite_and_invalid_table(app):
    from backend.services.code import middleware_service

    project = _make_project("u1")
    _make_deployment(project)  # no db_name → sqlite-local, not introspectable
    client = app.test_client()

    res = client.get(f"/api/code/apps/{project.id}/database", headers=_auth("u1"))
    assert res.status_code == 200
    assert res.get_json()["data"]["available"] is False

    # sample_rows rejects a non-identifier table name (injection guard) before any connect.
    assert middleware_service.sample_rows("postgresql://x/y", "users; DROP TABLE", 5)["available"] is False
    assert middleware_service.project_database_url(None) is None
