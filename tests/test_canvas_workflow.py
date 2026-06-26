"""
Integration tests for the ``code_canvas_generation`` workflow.

Runs the workflow synchronously against an in-memory DB with a fake text
provider (no network). Verifies: source content reaches the agent prompt, agent
conclusions land as artifacts + stage versions, and branch nodes prune the
unselected downstream subgraph.
"""
import pytest

from backend.app import create_app
from backend.extensions import db
from backend.services.ai.base import ImageGenerationResult, TextGenerationResult


@pytest.fixture
def app():
    application = create_app("testing")
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


class _FakeProvider:
    """Minimal text provider: echoes a fixed reply, records prompts."""

    provider_name = "claude"
    model = "fake-model"

    def __init__(self, reply="派生结论"):
        self._reply = reply
        self.last_prompt = None
        self.prompts = []

    def generate_text_stream(self, prompt, images=None):
        self.last_prompt = prompt
        self.prompts.append(prompt)
        yield self._reply

    def generate_text(self, prompt, images=None):
        self.last_prompt = prompt
        return TextGenerationResult(text=self._reply, success=True)

    def generate_image(self, *args, **kwargs):
        return ImageGenerationResult(image_data=None, success=False, error="no")


def _make_project(user_id="u1"):
    from backend.models.code import CodeProject, CodeProjectStatus

    project = CodeProject(
        user_id=user_id,
        title="待办应用",
        requirement_input="做一个待办应用",
        requirements_doc="# 需求\n做一个可勾选的待办清单",
        development_flow="# 流程\n创建-勾选-删除",
        style_prompt="简洁风格",
        status=CodeProjectStatus.REQUIREMENT_READY,
    )
    db.session.add(project)
    db.session.commit()
    return project


def _make_canvas(project, nodes, edges):
    from backend.models.code import CodeCanvas

    canvas = CodeCanvas(project_id=project.id, user_id=project.user_id, name="c")
    canvas.set_nodes(nodes)
    canvas.set_edges(edges)
    db.session.add(canvas)
    db.session.commit()
    return canvas


def _make_run(project, canvas):
    from backend.models.agent import AgentRun, AgentRunStatus

    run = AgentRun(
        user_id=project.user_id,
        domain="code",
        workflow="code_canvas_generation",
        resource_type="code_project",
        resource_id=project.id,
        status=AgentRunStatus.QUEUED,
    )
    run.set_config({"canvas_id": canvas.id})
    db.session.add(run)
    db.session.commit()
    return run


def _run(run):
    from backend.services.agent.recorder import RunRecorder
    from backend.services.agent.schemas import AgentContext
    from backend.services.agent.workflows.code_canvas_workflow import (
        run_code_canvas_generation,
    )

    ctx = AgentContext(
        run_id=run.id,
        user_id=run.user_id,
        team_id=None,
        domain="code",
        workflow="code_canvas_generation",
        resource_type="code_project",
        resource_id=run.resource_id,
        config=run.get_config(),
        input_snapshot={},
    )
    return run_code_canvas_generation(ctx, RunRecorder(run.id))


def _approve(run, stage):
    """Approve a paused review gate and resume the canvas run."""
    cfg = run.get_config()
    cfg["_resume"] = {"action": "approve", "stage": stage, "instruction": ""}
    run.set_config(cfg)
    db.session.commit()
    return _run(run)


def test_source_to_agent_produces_artifact_and_passes_input(app, monkeypatch):
    import backend.services.agent.canvas_nodes as cn

    fake = _FakeProvider("竞品分析结论")
    monkeypatch.setattr(cn, "build_text_provider", lambda **kw: fake)

    project = _make_project()
    nodes = [
        {
            "id": "s1",
            "type": "source_doc",
            "data": {"label": "需求", "config": {"source_kind": "requirements_doc"}},
        },
        {
            "id": "a1",
            "type": "agent",
            "data": {
                "label": "竞品分析",
                "config": {
                    "prompt": "基于需求做竞品分析",
                    "output_target": {
                        "as_artifact": True,
                        "as_stage_version": {"stage": "flow"},
                    },
                },
            },
        },
    ]
    edges = [{"id": "e1", "source": "s1", "target": "a1", "data": {"order": 0}}]
    run = _make_run(project, _make_canvas(project, nodes, edges))

    result = _run(run)
    assert result["status"] == "completed"

    from backend.models.agent import AgentArtifact

    artifacts = AgentArtifact.query.filter_by(run_id=run.id).all()
    assert any(
        a.domain_ref_type == "code_canvas_node" and a.content_text == "竞品分析结论"
        for a in artifacts
    )
    # The source doc content must have reached the agent prompt.
    assert "可勾选的待办清单" in (fake.last_prompt or "")

    # output_target.as_stage_version landed a new current FLOW version (import).
    from backend.models.code import CodeStage, CodeStageVersion

    versions = CodeStageVersion.query.filter_by(
        project_id=project.id, stage=CodeStage.FLOW
    ).all()
    assert any(v.source == "import" and v.is_current for v in versions)


def test_branch_prunes_unselected_downstream(app, monkeypatch):
    import backend.services.agent.canvas_nodes as cn

    monkeypatch.setattr(cn, "build_text_provider", lambda **kw: _FakeProvider("x"))

    project = _make_project()
    nodes = [
        {
            "id": "s1",
            "type": "source_doc",
            "data": {"label": "需求", "config": {"source_kind": "requirements_doc"}},
        },
        {
            "id": "b1",
            "type": "branch",
            "data": {
                "label": "分支",
                "config": {
                    "mode": "keyword",
                    "branches": [
                        {"key": "yes", "label": "命中", "keywords": ["待办"]},
                        {"key": "no", "label": "未命中", "keywords": ["不存在词"]},
                    ],
                    "default_branch": "no",
                },
            },
        },
        {"id": "ay", "type": "agent", "data": {"label": "命中路径", "config": {"prompt": "p"}}},
        {"id": "an", "type": "agent", "data": {"label": "未命中路径", "config": {"prompt": "p"}}},
    ]
    edges = [
        {"id": "e1", "source": "s1", "target": "b1", "data": {"order": 0}},
        {"id": "e2", "source": "b1", "target": "ay", "sourceHandle": "yes", "data": {"order": 0}},
        {"id": "e3", "source": "b1", "target": "an", "sourceHandle": "no", "data": {"order": 0}},
    ]
    run = _make_run(project, _make_canvas(project, nodes, edges))

    result = _run(run)
    assert result["status"] == "completed"

    from backend.models.agent import AgentStep

    steps = {s.agent_key: s for s in AgentStep.query.filter_by(run_id=run.id).all()}
    assert steps["b1"].status == "completed"
    assert steps["ay"].status == "completed"  # selected branch ran
    assert steps["an"].status == "skipped"  # unselected branch pruned


def test_typed_stage_chain_runs_and_lands_documents(app, monkeypatch):
    """A typed requirements -> flow chain runs both stages with pinned prompts,
    passing the upstream output downstream and landing typed CodeDocuments."""
    import backend.services.agent.canvas_nodes as cn

    fake = _FakeProvider("阶段产出内容")
    monkeypatch.setattr(cn, "build_text_provider", lambda **kw: fake)

    project = _make_project()
    nodes = [
        {
            "id": "s1",
            "type": "source_doc",
            "data": {"label": "需求源", "config": {"source_kind": "requirements_doc"}},
        },
        {
            "id": "R",
            "type": "stage",
            "data": {"label": "需求阶段", "config": {"contract_key": "requirements"}},
        },
        {
            "id": "F",
            "type": "stage",
            "data": {"label": "流程阶段", "config": {"contract_key": "flow"}},
        },
    ]
    edges = [
        # untyped source feeds the required user_text "brief" (not type-checked)
        {"id": "e1", "source": "s1", "target": "R", "targetHandle": "brief", "data": {"order": 0}},
        # typed edge: requirements.doc (code:requirements_doc) -> flow.requirements (match)
        {
            "id": "e2",
            "source": "R",
            "target": "F",
            "sourceHandle": "doc",
            "targetHandle": "requirements",
            "data": {"order": 0},
        },
    ]
    run = _make_run(project, _make_canvas(project, nodes, edges))

    # requirements and flow are both review-gated → drive through both gates.
    assert _run(run)["status"] == "paused"  # paused at requirements
    assert _approve(run, "R")["status"] == "paused"  # paused at flow
    assert _approve(run, "F")["status"] == "completed"

    from backend.models.agent import AgentStep
    from backend.models.code import CodeDocument

    steps = {s.agent_key: s for s in AgentStep.query.filter_by(run_id=run.id).all()}
    assert steps["R"].status == "completed"
    assert steps["F"].status == "completed"

    # Each typed stage landed a CodeDocument typed by its contract node_type.
    doc_types = {d.document_type for d in CodeDocument.query.filter_by(project_id=project.id).all()}
    assert {"requirements", "flow"} <= doc_types

    # The requirements stage used its PINNED prompt (default HEAD here): the
    # bundled requirements prompt body reached the model, and the source content
    # arrived as the typed upstream input.
    assert any("可勾选的待办清单" in p for p in fake.prompts)
    # Typed PortValue routing: the requirements doc arrived specifically on flow's
    # "requirements" input port (labeled by port name), not concat-of-everything.
    assert any("## requirements" in p and "阶段产出内容" in p for p in fake.prompts)

    # Binding persistence (§7): each stage step records its typed data lineage.
    fb = steps["F"].get_port_bindings()
    assert fb["node_type"] == "flow"
    assert fb["inputs"]["requirements"]["ref_kind"] == "code_document"  # consumed R's doc by ref
    assert fb["outputs"]["doc"]["ref_kind"] == "code_document"  # produced its own doc ref


def test_typed_preview_stage_generates_image_artifacts(app, monkeypatch):
    """A preview stage node turns upstream style text into IMAGE artifacts."""

    class _FakeGenService:
        def generate_preview_images(self, prompt, count=2, on_image=None, **kw):
            images = []
            for i in range(count):
                img = {"id": f"p{i}"}
                if on_image:
                    on_image(i, img, b"\x89PNG-fake-bytes")
                images.append(img)
            return images

    monkeypatch.setattr(
        "backend.services.code.get_code_generation_service", lambda: _FakeGenService()
    )

    project = _make_project()
    nodes = [
        {
            "id": "s1",
            "type": "source_doc",
            "data": {"label": "风格源", "config": {"source_kind": "style_prompt"}},
        },
        {
            "id": "P",
            "type": "stage",
            "data": {"label": "预览", "config": {"contract_key": "preview"}},
        },
    ]
    edges = [
        {"id": "e1", "source": "s1", "target": "P", "targetHandle": "style", "data": {"order": 0}}
    ]
    run = _make_run(project, _make_canvas(project, nodes, edges))

    result = _run(run)
    assert result["status"] == "completed"

    from backend.models.agent import AgentArtifact, AgentStep

    steps = {s.agent_key: s for s in AgentStep.query.filter_by(run_id=run.id).all()}
    assert steps["P"].status == "completed"
    images = AgentArtifact.query.filter_by(run_id=run.id, artifact_type="image").all()
    assert len(images) == 2
    assert all(a.domain_ref_id == "P" for a in images)


def test_typed_deploy_stage_runs_via_deploy_service(app, monkeypatch):
    """A deploy stage node brings up the backend via deploy_service and emits a
    typed deployment reference — powering the 'just (re)deploy the backend' case."""
    import backend.services.code.deploy_service as ds

    project = _make_project()

    class _FakeDep:
        id = "dep-1"
        deploy_run_id = None

    monkeypatch.setattr(
        ds,
        "deploy",
        lambda *a, **k: {
            "success": True,
            "status": "running",
            "api_base": f"/app/{project.id}/api",
            "preview_url": f"/preview/{project.id}/",
            "container": "c1",
            "image": "img1",
        },
    )
    monkeypatch.setattr(ds, "get_deployment", lambda pid: _FakeDep())

    nodes = [
        {
            "id": "src",
            "type": "source_doc",
            "data": {"label": "现有后端", "config": {"source_kind": "existing_backend"}},
        },
        {
            "id": "D",
            "type": "stage",
            "data": {"label": "部署", "config": {"contract_key": "deploy"}},
        },
    ]
    edges = [
        {"id": "e1", "source": "src", "target": "D", "targetHandle": "backend", "data": {"order": 0}}
    ]
    run = _make_run(project, _make_canvas(project, nodes, edges))

    result = _run(run)
    assert result["status"] == "completed"

    from backend.models.agent import AgentArtifact, AgentStep

    steps = {s.agent_key: s for s in AgentStep.query.filter_by(run_id=run.id).all()}
    assert steps["D"].status == "completed"
    # Emitted a typed deployment reference, recorded in the step binding.
    binding = steps["D"].get_port_bindings()
    assert binding["node_type"] == "deploy"
    assert binding["outputs"]["deployment"]["ref_kind"] == "code_deployment"
    # A deploy-meta JSON artifact was attached to the node.
    metas = AgentArtifact.query.filter_by(run_id=run.id, artifact_type="json").all()
    assert any(a.domain_ref_id == "D" for a in metas)


def test_review_gated_stage_pauses_then_resumes(app, monkeypatch):
    """A review-gated stage node (requirements) pauses the canvas run for confirmation;
    approving resumes past it and runs the downstream node."""
    import backend.services.agent.canvas_nodes as cn
    from backend.models.agent import AgentRun, AgentRunStatus, AgentStep

    fake = _FakeProvider("阶段产出")
    monkeypatch.setattr(cn, "build_text_provider", lambda **kw: fake)

    project = _make_project()
    nodes = [
        {
            "id": "src",
            "type": "source_doc",
            "data": {"label": "需求源", "config": {"source_kind": "requirements_doc"}},
        },
        {"id": "R", "type": "stage", "data": {"label": "需求", "config": {"contract_key": "requirements"}}},
        {"id": "F", "type": "stage", "data": {"label": "流程", "config": {"contract_key": "flow"}}},
    ]
    edges = [
        {"id": "e1", "source": "src", "target": "R", "targetHandle": "brief", "data": {"order": 0}},
        {
            "id": "e2",
            "source": "R",
            "target": "F",
            "sourceHandle": "doc",
            "targetHandle": "requirements",
            "data": {"order": 0},
        },
    ]
    run = _make_run(project, _make_canvas(project, nodes, edges))

    # First pass: requirements is review-gated → pause AFTER it, BEFORE flow.
    assert _run(run)["status"] == AgentRunStatus.PAUSED

    db.session.expire_all()
    run = db.session.get(AgentRun, run.id)
    assert run.get_progress().get("review_stage") == "R"
    steps = {s.agent_key: s for s in AgentStep.query.filter_by(run_id=run.id).all()}
    assert steps["R"].status == "completed"
    assert "F" not in steps  # downstream not run yet

    # Approve R → flow runs and is ALSO review-gated → pauses at F.
    assert _approve(run, "R")["status"] == AgentRunStatus.PAUSED
    db.session.expire_all()
    run = db.session.get(AgentRun, run.id)
    assert run.get_progress().get("review_stage") == "F"

    # Approve F → completes.
    assert _approve(run, "F")["status"] == AgentRunStatus.COMPLETED
    steps = {s.agent_key: s for s in AgentStep.query.filter_by(run_id=run.id).all()}
    assert steps["F"].status == "completed"
    # The resume snapshot is cleared on completion.
    assert "_canvas_state" not in db.session.get(AgentRun, run.id).get_config()


def test_ledger_writeback_extracts_requirements():
    """The requirements writeback folds the produced doc's establishments into the ledger."""
    from backend.services.agent.context_ledger import ContextLedger
    from backend.services.agent.ledger_writeback import merge_stage_doc_into_ledger

    ledger = ContextLedger.empty()
    doc = "## 产品定位\n智能待办应用\n\n## 功能范围\n- FR1: 创建待办\n- FR2: 勾选完成\n\n## 目标用户\n- 个人用户"
    assert merge_stage_doc_into_ledger("requirements", doc, ledger, source_step="canvas:R") is True
    assert ledger.project["one_liner"] == "智能待办应用"
    assert not ledger.is_empty()


def test_canvas_stage_writes_back_to_ledger(app, monkeypatch):
    """A requirements stage node folds its produced doc into the run's consensus ledger,
    so the establishment survives the review-gate pause and downstream nodes see it."""
    import backend.services.agent.canvas_nodes as cn
    from backend.models.agent import AgentRun

    structured = "## 产品定位\n智能待办\n\n## 功能范围\n- FR1: 创建\n\n## 目标用户\n- 个人"
    monkeypatch.setattr(cn, "build_text_provider", lambda **kw: _FakeProvider(structured))

    project = _make_project()
    nodes = [
        {
            "id": "src",
            "type": "source_doc",
            "data": {"label": "需求源", "config": {"source_kind": "requirements_doc"}},
        },
        {"id": "R", "type": "stage", "data": {"label": "需求", "config": {"contract_key": "requirements"}}},
    ]
    edges = [
        {"id": "e1", "source": "src", "target": "R", "targetHandle": "brief", "data": {"order": 0}}
    ]
    run = _make_run(project, _make_canvas(project, nodes, edges))
    assert _run(run)["status"] == "paused"  # requirements is review-gated

    db.session.expire_all()
    run = db.session.get(AgentRun, run.id)
    # one_liner established from the PRODUCED doc (not the seed) → writeback happened.
    assert run.get_context_ledger()["project"]["one_liner"] == "智能待办"


def test_typed_be_build_stage_generates_backend(app, monkeypatch):
    """A be_build stage node generates a backend project (reusing backend_project_service)
    and publishes it with the linear domain_ref so a deploy node can pick it up."""
    import backend.services.agent.workflows.code_backend_project_workflow as bw
    import backend.services.code.backend_project_service as bps
    from backend.services.agent.context_ledger import ContextLedger

    monkeypatch.setattr(bw, "_load_shared", lambda project, uid, tid: ({}, {}, ContextLedger.empty()))

    class _FakeBeService:
        def build_project(self, **kw):
            return {
                "success": True,
                "files": {"main.py": "print(1)", "Dockerfile": "FROM python:3.12-slim"},
                "stack": "python",
                "build_state": "green",
            }

    monkeypatch.setattr(bps, "get_backend_project_service", lambda: _FakeBeService())

    project = _make_project()
    nodes = [
        {
            "id": "src",
            "type": "source_doc",
            "data": {"label": "契约", "config": {"source_kind": "existing_contract"}},
        },
        {
            "id": "B",
            "type": "stage",
            "data": {"label": "后端", "config": {"contract_key": "be_build"}},
        },
    ]
    edges = [
        {"id": "e1", "source": "src", "target": "B", "targetHandle": "api_contract", "data": {"order": 0}}
    ]
    run = _make_run(project, _make_canvas(project, nodes, edges))

    result = _run(run)
    assert result["status"] == "completed"

    from backend.models.agent import AgentArtifact, AgentStep

    steps = {s.agent_key: s for s in AgentStep.query.filter_by(run_id=run.id).all()}
    assert steps["B"].status == "completed"
    zips = AgentArtifact.query.filter_by(
        run_id=run.id, domain_ref_type="code_backend_project_zip"
    ).all()
    assert len(zips) == 1
    assert zips[0].domain_ref_id == project.id  # linear domain_ref → deployable
    assert steps["B"].get_port_bindings()["outputs"]["backend"]["type"] == "code:backend_project"


def test_deploy_finds_canvas_generated_backend(app):
    """deploy_service falls back to a canvas-published backend zip when no dedicated
    backend-generation run exists (additive: linear behavior unchanged)."""
    from backend.models.agent import AgentArtifact, AgentArtifactType, AgentRun, AgentRunStatus
    from backend.services.code import deploy_service as ds

    project = _make_project()
    canvas_run = AgentRun(
        user_id=project.user_id,
        domain="code",
        workflow="code_canvas_generation",
        resource_type="code_project",
        resource_id=project.id,
        status=AgentRunStatus.COMPLETED,
    )
    db.session.add(canvas_run)
    db.session.commit()
    db.session.add(
        AgentArtifact(
            run_id=canvas_run.id,
            artifact_type=AgentArtifactType.TEXT,
            title="be",
            domain_ref_type="code_backend_project_zip",
            domain_ref_id=project.id,
        )
    )
    db.session.commit()

    found = ds._latest_backend_run(project.id, project.user_id)
    assert found is not None
    assert found.id == canvas_run.id


def test_typed_fe_build_stage_generates_frontend(app, monkeypatch):
    """A fe_build stage node generates a frontend project + previewable dist, published
    with the linear domain_ref so deploy/preview pick it up."""
    import backend.services.code.frontend_project_service as fps

    class _FakeFeService:
        def build_project(self, **kw):
            # The real service returns bytes (binary-safe); mirror that.
            return {
                "success": True,
                "files": {"src/App.tsx": b"x", "package.json": b"{}"},
                "dist_files": {"index.html": b"<html></html>"},
            }

    monkeypatch.setattr(fps, "get_frontend_project_service", lambda: _FakeFeService())

    project = _make_project()
    nodes = [
        {
            "id": "src",
            "type": "source_doc",
            "data": {"label": "预览源", "config": {"source_kind": "preview"}},
        },
        {"id": "FE", "type": "stage", "data": {"label": "前端", "config": {"contract_key": "fe_build"}}},
    ]
    edges = [
        {"id": "e1", "source": "src", "target": "FE", "targetHandle": "preview", "data": {"order": 0}}
    ]
    run = _make_run(project, _make_canvas(project, nodes, edges))
    assert _run(run)["status"] == "completed"

    from backend.models.agent import AgentArtifact, AgentStep

    steps = {s.agent_key: s for s in AgentStep.query.filter_by(run_id=run.id).all()}
    assert steps["FE"].status == "completed"
    zips = AgentArtifact.query.filter_by(
        run_id=run.id, domain_ref_type="code_frontend_project_zip"
    ).all()
    assert len(zips) == 1 and zips[0].domain_ref_id == project.id
    assert steps["FE"].get_port_bindings()["outputs"]["frontend"]["type"] == "code:frontend_project"


def test_typed_mw_provision_stage_generates_middleware(app, monkeypatch):
    """A mw_provision stage node generates the data layer, published with the linear
    domain_ref so the deploy node applies it."""
    import backend.services.agent.workflows.code_middleware_workflow as mw
    import backend.services.code.middleware_service as mws
    from backend.services.agent.context_ledger import ContextLedger

    monkeypatch.setattr(mw, "_load_shared", lambda project, uid, tid: ({}, {}, ContextLedger.empty()))
    monkeypatch.setattr(
        mws,
        "generate_data_layer",
        lambda project, manifest, summary: {
            "entities": [{"name": "todos"}],
            "init_sql": "CREATE TABLE todos(id int);",
            "seed_sql": "",
        },
    )

    project = _make_project()
    nodes = [
        {
            "id": "src",
            "type": "source_doc",
            "data": {"label": "契约", "config": {"source_kind": "existing_contract"}},
        },
        {"id": "MW", "type": "stage", "data": {"label": "中间件", "config": {"contract_key": "mw_provision"}}},
    ]
    edges = [
        {"id": "e1", "source": "src", "target": "MW", "targetHandle": "api_contract", "data": {"order": 0}}
    ]
    run = _make_run(project, _make_canvas(project, nodes, edges))
    assert _run(run)["status"] == "completed"

    from backend.models.agent import AgentArtifact, AgentStep

    steps = {s.agent_key: s for s in AgentStep.query.filter_by(run_id=run.id).all()}
    assert steps["MW"].status == "completed"
    metas = AgentArtifact.query.filter_by(
        run_id=run.id, domain_ref_type="code_middleware_meta"
    ).all()
    assert len(metas) == 1 and metas[0].domain_ref_id == project.id
    assert steps["MW"].get_port_bindings()["outputs"]["middleware"]["type"] == "code:middleware_manifest"


def test_typed_stage_with_bad_wiring_is_rejected(app, monkeypatch):
    """A type-mismatched typed edge fails the run at the validation gate."""
    import backend.services.agent.canvas_nodes as cn

    monkeypatch.setattr(cn, "build_text_provider", lambda **kw: _FakeProvider("x"))

    project = _make_project()
    nodes = [
        {
            "id": "R",
            "type": "stage",
            "data": {"label": "需求", "config": {"contract_key": "requirements"}},
        },
        {
            "id": "D",
            "type": "stage",
            "data": {"label": "文档", "config": {"contract_key": "documents"}},
        },
    ]
    # requirements.doc (code:requirements_doc) -> documents.flow (expects development_flow)
    edges = [
        {
            "id": "e1",
            "source": "R",
            "target": "D",
            "sourceHandle": "doc",
            "targetHandle": "flow",
            "data": {"order": 0},
        }
    ]
    run = _make_run(project, _make_canvas(project, nodes, edges))

    with pytest.raises(ValueError, match="画布校验未通过"):
        _run(run)
