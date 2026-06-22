"""
Code domain workflow — ``code_canvas_generation`` (n8n-style remix canvas).

Executes a user-authored node graph once: existing stage products are pre-filled
as read-only *source* inputs, then agent / merge / branch nodes run in topological
order, each consuming its wired-in upstream outputs and producing a new artifact.
Branch nodes prune the unselected downstream subgraphs. Agent conclusions can
optionally land as CodeDocuments or CodeStageVersions (reusing the version trail).

This is a pure overlay on the linear pipeline: it reloads the consensus ledger
from the latest ``code_full_generation`` run so derived conclusions stay on-spec,
but never mutates the linear workflow.
"""
import logging

from sqlalchemy import func

from backend.extensions import db
from backend.models.agent import (
    AgentArtifactType,
    AgentEventLevel,
    AgentEventType,
    AgentRun,
    AgentRunStatus,
)
from backend.models.code import (
    CodeCanvas,
    CodeCanvasNodeType,
    CodeDocument,
    CodeProject,
    CodeStage,
    CodeStageVersionSource,
)
from backend.services import pricing
from backend.services.agent.canvas_nodes import run_agent_node, run_branch_node, run_merge_node
from backend.services.agent.context_ledger import ContextLedger, seed_from_inputs
from backend.services.agent.dag_engine import CanvasGraph, NodeResult
from backend.services.code.version_service import safe_record_stage_version
from backend.services.credit_service import charge

logger = logging.getLogger(__name__)

_ROLE_FOR_TYPE = {"agent": "generator", "merge": "publisher", "branch": "critic"}

# Only text-primary stages can be safely captured into the version trail
# (record_stage_version reads the project's live field for that stage).
_TEXT_STAGES = (CodeStage.REQUIREMENTS, CodeStage.FLOW, CodeStage.STYLE)
_STAGE_FIELD = {
    CodeStage.REQUIREMENTS: "requirements_doc",
    CodeStage.FLOW: "development_flow",
    CodeStage.STYLE: "style_prompt",
}


def _source_content(project: CodeProject, config: dict) -> str:
    """Resolve a source node's referenced stage product (read live from the DB)."""
    kind = (config or {}).get("source_kind")
    if kind == "requirements_doc":
        return project.requirements_doc or ""
    if kind == "development_flow":
        return project.development_flow or ""
    if kind == "style_prompt":
        return project.style_prompt or ""
    if kind == "preview":
        return project.ui_baseline_prompt or project.confirmed_preview_url or ""
    if kind == "code_document":
        doc = CodeDocument.query.filter_by(
            id=config.get("document_id"), project_id=project.id
        ).first()
        return (doc.content if doc else "") or ""
    return ""


def _land_code_document(project, node, content: str, document_type: str) -> None:
    """Persist a node's conclusion as a CodeDocument (dedup by type + node label)."""
    existing = CodeDocument.query.filter_by(
        project_id=project.id, document_type=document_type, title=node.label
    ).first()
    if existing:
        existing.content = content
    else:
        max_order = (
            db.session.query(func.max(CodeDocument.order_index))
            .filter_by(project_id=project.id)
            .scalar()
            or 0
        )
        db.session.add(
            CodeDocument(
                project_id=project.id,
                document_type=document_type,
                title=node.label,
                content=content,
                prompt_expert=(node.config or {}).get("prompt", "") or "",
                order_index=max_order + 1,
            )
        )
    db.session.commit()


def _land_stage_version(project, node, content: str, stage: str, ctx, step) -> None:
    """Record a node's conclusion as the current version of a text stage.

    The version trail captures the project's live field, so the field must be
    written first. The previous content is preserved in history (rollback-able).
    """
    if stage not in _TEXT_STAGES:
        return
    setattr(project, _STAGE_FIELD[stage], content)
    db.session.commit()
    safe_record_stage_version(
        project,
        stage,
        source=CodeStageVersionSource.IMPORT,
        run_id=ctx.run_id,
        step_id=step.id,
        note=f"画布节点 {node.label}",
    )


def _maybe_land_outputs(node, content: str, project, ctx, step) -> None:
    """Optionally persist an agent node's conclusion as a project product."""
    target = (node.config or {}).get("output_target") or {}
    doc_cfg = target.get("as_code_document")
    if isinstance(doc_cfg, dict) and doc_cfg.get("document_type"):
        _land_code_document(project, node, content, doc_cfg["document_type"])
    ver_cfg = target.get("as_stage_version")
    if isinstance(ver_cfg, dict) and ver_cfg.get("stage"):
        _land_stage_version(project, node, content, ver_cfg["stage"], ctx, step)


def run_code_canvas_generation(ctx, recorder) -> dict:
    """Execute one canvas: topological run of agent / merge / branch nodes."""
    if not ctx.resource_id:
        raise ValueError("缺少 resource_id：画布执行需要一个 Code 项目")
    project = CodeProject.query.filter_by(id=ctx.resource_id, user_id=ctx.user_id).first()
    if not project:
        raise ValueError("项目不存在或无权访问")
    canvas_id = (ctx.config or {}).get("canvas_id")
    canvas = (
        CodeCanvas.query.filter_by(id=canvas_id, project_id=project.id).first()
        if canvas_id
        else None
    )
    if not canvas:
        raise ValueError("画布不存在")
    project_id = project.id

    graph = CanvasGraph(canvas.get_nodes(), canvas.get_edges())
    order = graph.topo_order()  # raises ValueError on a cycle

    # Reload the consensus ledger from the latest full-generation run (separate
    # run → no shared in-memory ledger). Seed from the project if there is none.
    prior = (
        AgentRun.query.filter_by(resource_id=project_id, workflow="code_full_generation")
        .order_by(AgentRun.created_at.desc())
        .first()
    )
    ledger = ContextLedger.load(prior.get_context_ledger() if prior else None)
    if ledger.is_empty():
        ledger = seed_from_inputs(
            project.requirement_input, project.title, project.get_selected_style_ids()
        )
    run = db.session.get(AgentRun, ctx.run_id)
    run.set_context_ledger(ledger.to_dict())
    db.session.commit()

    executable = [
        nid for nid in order if graph.nodes[nid].type in CodeCanvasNodeType.EXECUTABLE
    ]
    total = len(executable)
    completed = 0
    failed = 0
    extra_credits = 0

    outputs: dict[str, str] = {}
    node_active_handles: dict[str, set | None] = {}
    skipped: set[str] = set()

    # Pre-fill source node outputs (read-only inputs; not executed as steps).
    for nid in order:
        node = graph.nodes[nid]
        if node.type == CodeCanvasNodeType.SOURCE_DOC:
            outputs[nid] = _source_content(project, node.config)

    def progress(current: str) -> None:
        r = db.session.get(AgentRun, ctx.run_id)
        if r:
            r.set_progress(
                {
                    "total_steps": total,
                    "completed_steps": completed,
                    "failed_steps": failed,
                    "current_step": current,
                }
            )
            db.session.commit()
        recorder.emit(
            AgentEventType.PROGRESS,
            message=f"进度 {completed}/{total}",
            payload={"completed": completed, "total": total, "current": current},
        )

    def edge_active(edge) -> bool:
        if edge.source in skipped:
            return False
        ah = node_active_handles.get(edge.source)
        if ah is None:
            return True
        return (edge.source_handle or "") in ah

    if total == 0:
        recorder.emit(
            AgentEventType.WARNING,
            level=AgentEventLevel.WARNING,
            message="画布没有可执行节点（仅有来源节点）",
        )
        return {"status": AgentRunStatus.COMPLETED, "resource_id": project_id, "extra_credits": 0}

    for index, nid in enumerate(executable, start=1):
        if ctx.is_cancelled():
            return {
                "status": AgentRunStatus.CANCELLED,
                "resource_id": project_id,
                "extra_credits": extra_credits,
            }
        node = graph.nodes[nid]
        role = _ROLE_FOR_TYPE.get(node.type, "generator")
        inc = graph.incoming(nid)
        active_edges = [e for e in inc if edge_active(e)]
        inputs = [(graph.nodes[e.source].label, outputs.get(e.source, "")) for e in active_edges]

        # Prune: a node whose every incoming branch was pruned is skipped, and its
        # own downstream is pruned in turn (it joins `skipped`).
        if inc and not active_edges:
            with recorder.step(nid, node.label, role, index, input_summary="上游已剪枝") as step:
                step.mark_skipped("所有上游分支均未激活")
            skipped.add(nid)
            outputs[nid] = ""
            progress(node.label)
            continue

        # Per-node charge (no-op while priced 0). Insufficient → skip + prune.
        if node.type == CodeCanvasNodeType.AGENT:
            if charge(
                user_id=ctx.user_id,
                amount=pricing.CODE_CANVAS_NODE,
                operation="code_canvas_node",
                resource_type="agent_run",
                resource_id=ctx.run_id,
                description=f"canvas node {node.label}",
                team_id=ctx.team_id,
            ):
                extra_credits += pricing.CODE_CANVAS_NODE
            else:
                with recorder.step(nid, node.label, role, index, input_summary="积分不足") as step:
                    step.mark_skipped("积分不足，跳过该节点")
                skipped.add(nid)
                outputs[nid] = ""
                progress(node.label)
                continue

        try:
            with recorder.step(
                nid, node.label, role, index, input_summary=f"{node.type} 节点"
            ) as step:
                injected = ledger.render_for_prompt()
                if node.type == CodeCanvasNodeType.AGENT:
                    result = run_agent_node(node, inputs, injected_ledger=injected, step=step)
                elif node.type == CodeCanvasNodeType.MERGE:
                    result = run_merge_node(node, inputs, step=step)
                elif node.type == CodeCanvasNodeType.BRANCH:
                    result = run_branch_node(node, inputs, injected_ledger=injected, step=step)
                else:
                    result = NodeResult()

                outputs[nid] = result.output_text
                node_active_handles[nid] = result.active_handles
                step.add_artifact(
                    AgentArtifactType.MARKDOWN,
                    node.label,
                    content_text=result.output_text,
                    filename=f"{nid}.md",
                    write_file=True,
                    domain_ref_type="code_canvas_node",
                    domain_ref_id=nid,
                )
                if node.type == CodeCanvasNodeType.AGENT:
                    _maybe_land_outputs(node, result.output_text, project, ctx, step)
                step.set_output(
                    output_summary=result.output_summary,
                    reasoning_summary=result.reasoning_summary,
                    self_check=result.self_check,
                    next_action="供下游节点消费或落库。",
                )
            completed += 1
        except Exception as exc:  # noqa: BLE001 - one node's failure must not abort the run
            failed += 1
            outputs[nid] = ""
            skipped.add(nid)  # prune this node's downstream just like a skip
            recorder.emit(
                AgentEventType.WARNING,
                level=AgentEventLevel.WARNING,
                message=f"节点「{node.label}」失败，已跳过其下游：{exc}",
                payload={"node_id": nid, "error": str(exc)},
            )
        progress(node.label)

    canvas.last_run_id = ctx.run_id
    db.session.commit()
    status = AgentRunStatus.PARTIAL if failed else AgentRunStatus.COMPLETED
    return {"status": status, "resource_id": project_id, "extra_credits": extra_credits}
