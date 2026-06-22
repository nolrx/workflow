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
    """Minimal text provider: echoes a fixed reply, records the last prompt."""

    provider_name = "claude"
    model = "fake-model"

    def __init__(self, reply="派生结论"):
        self._reply = reply
        self.last_prompt = None

    def generate_text_stream(self, prompt, images=None):
        self.last_prompt = prompt
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
