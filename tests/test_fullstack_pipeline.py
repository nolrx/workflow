"""
Unit tests for the full-stack pipeline (frontend + backend + middleware).

All network-free: the text provider is forced unavailable so contract /
data-layer synthesis takes its deterministic fallback path, and middleware
provisioning is exercised against the sqlite (dev) branch. Verifies the pure
logic the three concurrent workflows + the atomic deploy depend on:
  * shared contract synthesis (fallback extraction + persistence + render)
  * backend document digest filtering + middleware block rendering
  * per-project namespace naming / provisioning / sql splitting / teardown
"""
import pytest

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
    """Force the deterministic (no-AI) path for every test here."""
    monkeypatch.setattr("backend.services.ai.get_text_provider", lambda: None)


_FLOW = """# 开发流程

## 技术假设
后端采用 Node.js + Express,数据库用 PostgreSQL,热点用 Redis 缓存。

## 模块拆分
M1 任务管理(覆盖 FR1)。

## 数据设计
Task 实体:id、title、done;使用 PostgreSQL 存储,Redis 做会话缓存。

## 接口设计
GET /tasks 返回任务列表;POST /tasks 创建任务。

## 后端服务
单一 REST 服务,负责任务的增删查。

## AI/提示词链路
本项目不涉及 AI 调用。
"""


def _make_project(user_id="u1"):
    from backend.models.code import CodeDocument, CodeProject, CodeProjectStatus

    project = CodeProject(
        user_id=user_id,
        title="待办应用",
        requirement_input="做一个待办应用",
        requirements_doc="# 需求\nFR1 可创建/勾选任务。技术架构:Node + Postgres。",
        development_flow=_FLOW,
        style_prompt="简洁",
        status=CodeProjectStatus.DOCUMENTS_READY,
    )
    db.session.add(project)
    db.session.flush()
    # Backend-relevant + irrelevant docs, to exercise the digest filter.
    for dtype, title, content in [
        ("backend_spec", "后端规格", "REST 接口与错误码"),
        ("data_model", "数据模型", "Task 表结构"),
        ("frontend_spec", "前端规格", "页面与组件(应被过滤掉)"),
    ]:
        db.session.add(CodeDocument(
            project_id=project.id, document_type=dtype, title=title,
            content=content, prompt_expert="x", order_index=0,
        ))
    db.session.commit()
    return project


# --- contract synthesis ------------------------------------------------------
def test_fallback_contract_extracts_sections(app):
    from backend.services.code.fullstack import contract_service

    project = _make_project()
    contract = contract_service.synthesize_contract(project)

    assert contract["_degraded"] is True
    # api_summary carries the interface + data sections verbatim.
    assert "接口设计" in contract["api_summary"]
    assert "/tasks" in contract["api_summary"]
    # datastores inferred from keyword scan: postgres + redis cache.
    types = {d["type"] for d in contract["middleware"]["datastores"]}
    assert "postgres" in types
    assert contract["middleware"]["cache"] is not None
    assert contract["middleware"]["cache"]["type"] == "redis"
    # stack guessed from the tech-assumptions section.
    assert contract["tech_stack"]["language"] == "node"
    # env always carries PORT + DATABASE_URL.
    env_names = {e["name"] for e in contract["middleware"]["env"]}
    assert {"PORT", "DATABASE_URL"} <= env_names


def test_backend_documents_digest_filters(app):
    from backend.services.code.fullstack import contract_service

    project = _make_project()
    digest = contract_service.backend_documents_digest(project)
    assert "后端规格" in digest
    assert "数据模型" in digest
    assert "前端规格" not in digest  # frontend_spec excluded


def test_render_contract_prefers_openapi_paths():
    from backend.services.code.fullstack import contract_service

    contract = {
        "openapi": {"paths": {
            "/health": {"get": {"summary": "健康检查"}},
            "/tasks": {"get": {"summary": "列表"}, "post": {"summary": "创建"}},
        }},
        "tech_stack": {"language": "node", "framework": "express"},
    }
    block = contract_service.render_contract_for_prompt(contract)
    assert "GET /health" in block
    assert "GET /tasks" in block
    assert "POST /tasks" in block
    assert "node" in block and "express" in block


def test_render_contract_falls_back_to_summary():
    from backend.services.code.fullstack import contract_service

    block = contract_service.render_contract_for_prompt(
        {"openapi": {}, "api_summary": "## 接口设计\nGET /x 获取"}
    )
    assert "GET /x" in block


def test_ensure_contract_persists_ready_ledger(app):
    from backend.models.code.fullstack import ContractStatus
    from backend.services.code.fullstack import contract_service

    project = _make_project()
    row = contract_service.ensure_contract(project, user_id="u1", team_id=None)
    assert row.contract_status == ContractStatus.READY
    assert row.version == 1
    manifest = row.get_middleware_manifest()
    assert manifest.get("datastores")
    # The seed shared ledger is persisted for the three runs to branch from.
    assert isinstance(row.get_shared_ledger(), dict)

    # Idempotent: a second call without force returns the same ready row.
    row2 = contract_service.ensure_contract(project, user_id="u1", team_id=None)
    assert row2.version == 1


# --- middleware service ------------------------------------------------------
def test_sanitized_db_name_is_valid_identifier():
    from backend.services.code import middleware_service

    name = middleware_service._sanitized_db_name("11111111-2222-3333-4444-555555555555")
    assert name.startswith("app_")
    assert name.replace("app_", "").isalnum()
    assert len(name) <= 48


def test_md_section_extracts_named_section():
    from backend.services.code import middleware_service

    body = middleware_service._md_section(_FLOW, "数据设计")
    assert "Task 实体" in body
    assert "接口设计" not in body  # only the data-design section


def test_provision_namespace_sqlite_branch(app, monkeypatch):
    from backend.services.code import middleware_service

    monkeypatch.setenv("DATABASE_URL", "sqlite:///dev.db")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    result = middleware_service.provision_namespace("abc-123")
    assert result.applicable is True
    assert result.engine_kind == "sqlite"
    assert result.database_url.startswith("sqlite:")
    # teardown of a sqlite (no db_name) namespace is a no-op success.
    assert middleware_service.teardown_namespace(result.db_name) is True


def test_project_database_url_keeps_password():
    """Regression: the injected per-project URL must carry the REAL password.

    ``str(URL)`` masks it as ``***`` (→ a 28P01 auth failure in the deployed
    backend); the provisioned URL must render the actual credential.
    """
    from backend.services.code import middleware_service

    url = middleware_service._project_database_url(
        "postgresql://ai_studio:s3cr3t@postgres:5432/main", "app_abc123"
    )
    assert "s3cr3t" in url
    assert "***" not in url
    assert url.endswith("/app_abc123")


def test_split_sql_handles_statements_and_comments():
    from backend.services.code import middleware_service

    sql = "-- comment\nCREATE TABLE a (id int);\nINSERT INTO a VALUES (1);"
    stmts = middleware_service._split_sql(sql)
    assert len(stmts) == 2
    assert stmts[0].startswith("CREATE TABLE a")
    assert stmts[1].startswith("INSERT INTO a")


def test_generate_data_layer_fallback(app):
    from backend.services.code import middleware_service

    project = _make_project()
    manifest = {"datastores": [{"type": "postgres", "purpose": "主库"}]}
    layer = middleware_service.generate_data_layer(project, manifest, "## 接口设计\nGET /tasks")
    assert layer["_degraded"] is True
    assert "数据设计" in layer["init_sql"] or "数据" in layer["init_sql"]


# --- backend workflow helper -------------------------------------------------
def test_render_middleware_block():
    from backend.services.agent.workflows.code_backend_project_workflow import _render_middleware

    block = _render_middleware({
        "datastores": [{"type": "postgres", "purpose": "主库"}],
        "cache": {"type": "redis", "purpose": "缓存"},
        "env": [{"name": "PORT", "purpose": "端口"}, {"name": "DATABASE_URL", "purpose": "连接"}],
    })
    assert "postgres" in block
    assert "redis" in block
    assert "PORT" in block and "DATABASE_URL" in block


def test_render_middleware_empty():
    from backend.services.agent.workflows.code_backend_project_workflow import _render_middleware

    assert _render_middleware({}) == ""


def test_reconcile_orphaned_runs(app, monkeypatch):
    """On restart: a resumable run is re-dispatched (continues from its persisted
    phase), a crashed deploy is failed (side-effectful — not auto-resumed), and
    PAUSED/terminal runs are left untouched. (Detailed routing: test_agent_resume.)"""
    from datetime import datetime

    from backend.models.agent import AgentRun, AgentRunStatus
    from backend.services.agent import runtime as rt

    started: list[str] = []
    monkeypatch.setattr(rt.agent_runtime, "start", lambda app, run_id: started.append(run_id))

    def mk(workflow, status):
        run = AgentRun(
            user_id="u1", domain="code", workflow=workflow,
            status=status, credit_reserved=0, started_at=datetime.utcnow(),
        )
        db.session.add(run)
        db.session.commit()
        return run.id

    resumable = mk("code_full_generation", AgentRunStatus.RUNNING)
    deploy = mk("code_fullstack_deploy", AgentRunStatus.RUNNING)
    paused = mk("code_full_generation", AgentRunStatus.PAUSED)
    completed = mk("code_full_generation", AgentRunStatus.COMPLETED)

    n = rt.reconcile_orphaned_runs(app)
    assert n == 2  # the two RUNNING rows were handled (one resumed, one failed)
    db.session.expire_all()
    assert db.session.get(AgentRun, resumable).status == AgentRunStatus.RUNNING
    assert resumable in started  # re-dispatched, not lost
    assert db.session.get(AgentRun, deploy).status == AgentRunStatus.FAILED
    assert deploy not in started  # side-effectful: not auto-resumed
    assert db.session.get(AgentRun, paused).status == AgentRunStatus.PAUSED
    assert db.session.get(AgentRun, completed).status == AgentRunStatus.COMPLETED


# --- atomic deploy: rollback visibility + status parity ----------------------
def test_deploy_fail_rolls_back_narrates_and_keeps_status_in_lockstep(app):
    """A failed deploy that had provisioned resources: rolls them back (reverse
    order), narrates the rollback onto the timeline, and reports ROLLED_BACK
    consistently — the returned status equals the persisted status."""
    from backend.models.code.fullstack import CodeDeployment, DeploymentStatus
    from backend.services.code import deploy_service

    dep = CodeDeployment(project_id="p-roll", user_id="u1")
    db.session.add(dep)
    db.session.commit()

    undone = []
    rollback = [lambda: undone.append("db"), lambda: undone.append("image")]
    phases = []

    result = deploy_service._fail(
        dep, rollback, "镜像构建失败",
        narrate=lambda phase, message, payload=None: phases.append((phase, message)),
    )

    # #3: returned status matches the persisted status (both ROLLED_BACK).
    assert result["success"] is False
    assert result["status"] == DeploymentStatus.ROLLED_BACK
    assert dep.status == DeploymentStatus.ROLLED_BACK
    # rollback actions actually ran, in reverse (image before db).
    assert undone == ["image", "db"]
    # #2: the rollback is narrated onto the timeline (previously invisible).
    assert any(phase == "rollback" for phase, _ in phases)


def test_deploy_fail_without_provisioned_resources_is_plain_failed(app):
    """Failing before anything was provisioned reports FAILED (nothing to undo)
    and emits no misleading 'rollback' narration."""
    from backend.models.code.fullstack import CodeDeployment, DeploymentStatus
    from backend.services.code import deploy_service

    dep = CodeDeployment(project_id="p-early", user_id="u1")
    db.session.add(dep)
    db.session.commit()

    phases = []
    result = deploy_service._fail(
        dep, [], "中间件命名空间创建失败",
        narrate=lambda *args: phases.append(args),
    )
    assert result["status"] == DeploymentStatus.FAILED
    assert dep.status == DeploymentStatus.FAILED
    assert phases == []  # no rollback narration when there is nothing to roll back
