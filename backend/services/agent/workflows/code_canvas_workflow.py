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
    AgentArtifact,
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
from backend.services.agent.canvas_nodes import (
    PortInput,
    run_agent_node,
    run_branch_node,
    run_merge_node,
    run_stage_be_node,
    run_stage_deploy_node,
    run_stage_fe_node,
    run_stage_mw_node,
    run_stage_preview_node,
    run_stage_text_node,
)
from backend.services.agent.context_ledger import ContextLedger, seed_from_inputs
from backend.services.agent.contracts import WIRED_EXECUTORS, make_port_value, validate_graph
from backend.services.agent.contracts.defaults import get_default_contract
from backend.services.agent.dag_engine import CanvasGraph, NodeResult
from backend.services.agent.ledger_writeback import merge_stage_doc_into_ledger
from backend.services.code.version_service import safe_record_stage_version
from backend.services.credit_service import charge

logger = logging.getLogger(__name__)

_ROLE_FOR_TYPE = {
    "agent": "generator",
    "merge": "publisher",
    "branch": "critic",
    "stage": "generator",
}

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


# "Existing built product" source kinds → (typed port, ref_kind, source workflow).
# These let a user wire their CURRENT frontend/backend/contract into a deploy or
# build node and REUSE it instead of regenerating (e.g. "I already have a frontend,
# just (re)deploy the backend"). The workflow (when set) is used to find the latest
# built artifact for that lane.
_EXISTING_SOURCE_SPEC = {
    "existing_frontend": ("code:frontend_project", "agent_artifact", "code_frontend_project_generation"),
    "existing_backend": ("code:backend_project", "agent_artifact", "code_backend_project_generation"),
    "existing_contract": ("code:api_contract", "code_ledger_field", None),
    "existing_middleware": ("code:middleware_manifest", "code_ledger_field", None),
}


def _source_port_value(project, config: dict, nid: str):
    """Typed PortValue for an 'existing built product' source node, else None.

    ``ref_id`` is best-effort (the latest matching artifact); None is acceptable —
    the type alone makes the graph wireable and validates, and the consuming
    executor reads the project's current product at run time.
    """
    spec = _EXISTING_SOURCE_SPEC.get((config or {}).get("source_kind"))
    if not spec:
        return None
    port_type, ref_kind, workflow = spec
    ref_id = None
    if workflow:
        try:
            run = (
                AgentRun.query.filter_by(resource_id=project.id, workflow=workflow)
                .order_by(AgentRun.created_at.desc())
                .first()
            )
            if run:
                art = (
                    AgentArtifact.query.filter_by(run_id=run.id)
                    .order_by(AgentArtifact.created_at.desc())
                    .first()
                )
                ref_id = art.id if art else None
        except Exception:  # noqa: BLE001 — best-effort ref; the type marker is what matters
            ref_id = None
    return make_port_value(port_type, ref_kind, ref_id=ref_id, produced_by=nid)


def _input_value(port_outputs: dict, source: str, source_handle):
    """Resolve a typed input's PortValue from an upstream node's outputs.

    Tries the wired output handle; falls back to the upstream's sole output (source
    nodes expose a single unnamed handle, so an edge into a typed input carries a
    null sourceHandle). Returns None for a freeform/text-only upstream.
    """
    outs = port_outputs.get(source, {})
    if source_handle in outs:
        return outs[source_handle]
    return next(iter(outs.values())) if len(outs) == 1 else None


def _land_code_document(project, node, content: str, document_type: str):
    """Persist a node's conclusion as a CodeDocument (dedup by type + node label).

    Returns the CodeDocument so the caller can build a typed PortValue referencing it.
    """
    existing = CodeDocument.query.filter_by(
        project_id=project.id, document_type=document_type, title=node.label
    ).first()
    if existing:
        existing.content = content
        doc = existing
    else:
        max_order = (
            db.session.query(func.max(CodeDocument.order_index))
            .filter_by(project_id=project.id)
            .scalar()
            or 0
        )
        doc = CodeDocument(
            project_id=project.id,
            document_type=document_type,
            title=node.label,
            content=content,
            prompt_expert=(node.config or {}).get("prompt", "") or "",
            order_index=max_order + 1,
        )
        db.session.add(doc)
    db.session.commit()
    return doc


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

    # Typed gate: any STAGE (contract) node has its ports type-checked against its
    # upstream wiring. A pure freeform canvas (no contract_key on any node) yields
    # no errors, so this is a no-op for the existing agent/merge/branch flows.
    typed_errors = validate_graph(graph, get_default_contract)
    if typed_errors:
        raise ValueError("画布校验未通过：" + "；".join(typed_errors[:6]))

    # Consensus ledger. On a RESUME, keep the ledger THIS run already accumulated
    # across earlier nodes (so stage writebacks survive a review-gate pause); on a
    # fresh run, seed from the latest full-generation run (or the project inputs).
    run = db.session.get(AgentRun, ctx.run_id)
    own = ContextLedger.load(run.get_context_ledger())
    if not own.is_empty():
        ledger = own
    else:
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
        run.set_context_ledger(ledger.to_dict())
        db.session.commit()

    def _persist_ledger() -> None:
        r = db.session.get(AgentRun, ctx.run_id)
        if r:
            r.set_context_ledger(ledger.to_dict())
            db.session.commit()

    executable = [
        nid for nid in order if graph.nodes[nid].type in CodeCanvasNodeType.EXECUTABLE
    ]
    total = len(executable)

    # Resume support: a review-gated stage node pauses the run; the user approves
    # (continue past it) or revises (re-run it with an instruction). The one-shot
    # ``_resume`` directive + a snapshot of completed-node state live on the run
    # config (see ``_snapshot_state`` / ``pause_at_node``). A fresh run has neither,
    # so this is a no-op for freeform canvases.
    cfg = ctx.config or {}
    resume = cfg.get("_resume") or None
    saved = cfg.get("_canvas_state") or {}
    done: set[str] = set(saved.get("done") or [])
    revise_target = None
    if resume and resume.get("action") == "revise":
        revise_target = resume.get("stage")
        done.discard(revise_target)  # re-run the revised node
    if resume:  # consume the one-shot directive so a relaunch never repeats it
        _r = db.session.get(AgentRun, ctx.run_id)
        _c = _r.get_config()
        _c.pop("_resume", None)
        _r.set_config(_c)
        db.session.commit()

    completed = int(saved.get("completed") or 0)
    failed = 0
    extra_credits = 0

    outputs: dict[str, str] = dict(saved.get("outputs") or {})
    # Typed outputs per node: nid -> {output_port_name: PortValue}. Populated only
    # for typed stage nodes; freeform/source upstreams fall back to ``outputs`` text.
    port_outputs: dict[str, dict[str, dict]] = dict(saved.get("port_outputs") or {})
    node_active_handles: dict[str, set | None] = {
        k: (set(v) if v is not None else None)
        for k, v in (saved.get("active_handles") or {}).items()
    }
    skipped: set[str] = set(saved.get("skipped") or [])

    # Pre-fill source node outputs (read-only inputs; not executed as steps). An
    # "existing built product" source also emits a typed PortValue so it can feed a
    # typed deploy/build input (reuse instead of regenerate).
    for nid in order:
        node = graph.nodes[nid]
        if node.type == CodeCanvasNodeType.SOURCE_DOC:
            outputs[nid] = _source_content(project, node.config)
            existing_pv = _source_port_value(project, node.config, nid)
            if existing_pv:
                port_outputs[nid] = {"": existing_pv}

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

    def _snapshot_state() -> dict:
        """Serializable snapshot of completed-node state, stashed on pause so a
        resume can skip done nodes and reuse their outputs. Text outputs are capped
        to keep the run config bounded."""
        return {
            "done": sorted(done),
            "completed": completed,
            "outputs": {k: (v or "")[:20000] for k, v in outputs.items()},
            "port_outputs": port_outputs,
            "active_handles": {
                k: (list(v) if v is not None else None) for k, v in node_active_handles.items()
            },
            "skipped": sorted(skipped),
        }

    def pause_at_node(node_id: str, label: str) -> dict:
        """Pause the run for review of a review-gated stage node (reuses the generic
        ``/runs/<id>/resume`` mechanism: progress.review_stage + STEP_AWAITING_REVIEW)."""
        r = db.session.get(AgentRun, ctx.run_id)
        if r:
            c = r.get_config()
            c["_canvas_state"] = _snapshot_state()
            r.set_config(c)
            prog = r.get_progress() or {}
            prog.update({"review_stage": node_id, "current_step": node_id})
            r.set_progress(prog)
            db.session.commit()
        recorder.emit(
            AgentEventType.STEP_AWAITING_REVIEW,
            message=f"「{label}」已生成,请确认或提出调整意见",
            payload={"stage": node_id},
        )
        return {
            "status": AgentRunStatus.PAUSED,
            "resource_id": project_id,
            "extra_credits": extra_credits,
        }

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
        if nid in done:
            continue  # already completed before a pause; its output is reused from the snapshot
        role = _ROLE_FOR_TYPE.get(node.type, "generator")
        if node.type == CodeCanvasNodeType.STAGE:
            # A typed stage carries its own role (e.g. deploy = publisher); fall back
            # to the generic STAGE role only for an unknown contract.
            stage_role_contract = get_default_contract((node.config or {}).get("contract_key"))
            if stage_role_contract:
                role = stage_role_contract.role
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
        if node.type in (CodeCanvasNodeType.AGENT, CodeCanvasNodeType.STAGE):
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
                elif node.type == CodeCanvasNodeType.STAGE:
                    contract = get_default_contract((node.config or {}).get("contract_key"))
                    if contract is None or contract.executor not in WIRED_EXECUTORS:
                        raise RuntimeError(
                            f"阶段节点暂不支持执行:{(node.config or {}).get('contract_key')}"
                        )
                    # Resolve inputs BY PORT (target handle), carrying each upstream's
                    # typed PortValue ref when the upstream is itself a typed port.
                    stage_inputs = [
                        PortInput(
                            port=e.target_handle or "",
                            text=outputs.get(e.source, ""),
                            value=_input_value(port_outputs, e.source, e.source_handle),
                            label=graph.nodes[e.source].label,
                        )
                        for e in active_edges
                    ]
                    if contract.executor == "stage_preview":
                        result = run_stage_preview_node(node, stage_inputs, step=step)
                    elif contract.executor == "container_fe":
                        result = run_stage_fe_node(
                            node, stage_inputs, project=project, ctx=ctx,
                            recorder=recorder, step=step, injected_ledger=injected,
                        )
                    elif contract.executor == "container_be":
                        result = run_stage_be_node(
                            node, stage_inputs, project=project, ctx=ctx, recorder=recorder, step=step
                        )
                    elif contract.executor == "provision_mw":
                        result = run_stage_mw_node(
                            node, stage_inputs, project=project, ctx=ctx, recorder=recorder, step=step
                        )
                    elif contract.executor == "deploy":
                        result = run_stage_deploy_node(
                            node, stage_inputs, project=project, ctx=ctx, recorder=recorder, step=step
                        )
                    else:
                        revise = (
                            resume.get("instruction", "")
                            if (resume and nid == revise_target)
                            else ""
                        )
                        result = run_stage_text_node(
                            node, stage_inputs, contract=contract, injected_ledger=injected,
                            step=step, revise_instruction=revise,
                        )
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
                elif node.type == CodeCanvasNodeType.STAGE:
                    stage_contract = get_default_contract((node.config or {}).get("contract_key"))
                    out_port = (
                        stage_contract.outputs[0]
                        if stage_contract and stage_contract.outputs
                        else None
                    )
                    if stage_contract and stage_contract.executor == "stage_text" and out_port:
                        # Text stage → land a CodeDocument + emit a typed doc reference.
                        doc = _land_code_document(
                            project, node, result.output_text, stage_contract.node_type
                        )
                        port_outputs[nid] = {
                            out_port.name: make_port_value(
                                out_port.type, "code_document", ref_id=doc.id, produced_by=nid
                            )
                        }
                        # Fold what this stage established back into the consensus
                        # ledger so downstream stage nodes build on it (P1#4).
                        if merge_stage_doc_into_ledger(
                            stage_contract.node_type, result.output_text, ledger,
                            source_step=f"canvas:{nid}",
                        ):
                            _persist_ledger()
                    elif stage_contract and stage_contract.executor == "stage_preview" and out_port:
                        # Image stage → emit a typed artifact reference (no CodeDocument).
                        ids = result.extra.get("image_artifact_ids") or []
                        if ids:
                            port_outputs[nid] = {
                                out_port.name: make_port_value(
                                    out_port.type, "agent_artifact", ref_id=ids[0], produced_by=nid
                                )
                            }
                    elif stage_contract and stage_contract.executor == "container_fe" and out_port:
                        # Frontend build → emit a typed frontend_project artifact reference.
                        aid = result.extra.get("frontend_artifact_id")
                        if aid:
                            port_outputs[nid] = {
                                out_port.name: make_port_value(
                                    out_port.type, "agent_artifact", ref_id=aid, produced_by=nid
                                )
                            }
                    elif stage_contract and stage_contract.executor == "container_be" and out_port:
                        # Backend build → emit a typed backend_project artifact reference.
                        aid = result.extra.get("backend_artifact_id")
                        if aid:
                            port_outputs[nid] = {
                                out_port.name: make_port_value(
                                    out_port.type, "agent_artifact", ref_id=aid, produced_by=nid
                                )
                            }
                    elif stage_contract and stage_contract.executor == "provision_mw" and out_port:
                        # Middleware → emit a typed middleware_manifest artifact reference.
                        aid = result.extra.get("middleware_artifact_id")
                        if aid:
                            port_outputs[nid] = {
                                out_port.name: make_port_value(
                                    out_port.type, "agent_artifact", ref_id=aid, produced_by=nid
                                )
                            }
                    elif stage_contract and stage_contract.executor == "deploy" and out_port:
                        # Deploy stage → emit a typed deployment reference.
                        dep_id = result.extra.get("deployment_id")
                        if dep_id:
                            port_outputs[nid] = {
                                out_port.name: make_port_value(
                                    out_port.type, "code_deployment", ref_id=dep_id, produced_by=nid
                                )
                            }
                    # Record this stage's typed data lineage (typed inputs by port +
                    # outputs + prompt pin) onto the step for replayable provenance (§7).
                    step.set_port_bindings(
                        {
                            "node_id": nid,
                            "node_type": stage_contract.node_type if stage_contract else None,
                            "executor": stage_contract.executor if stage_contract else None,
                            "inputs": {
                                pi.port: pi.value or {"ref_kind": "inline_text", "type": None}
                                for pi in stage_inputs
                            },
                            "outputs": port_outputs.get(nid, {}),
                            "prompt_pin": (node.config or {}).get("prompt_pin"),
                        }
                    )
                step.set_output(
                    output_summary=result.output_summary,
                    reasoning_summary=result.reasoning_summary,
                    self_check=result.self_check,
                    next_action="供下游节点消费或落库。",
                )
            completed += 1
            done.add(nid)
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

        # Review gate: pause after a successfully-run review-gated stage node so the
        # user confirms or adjusts before downstream nodes consume its output.
        if nid in done and node.type == CodeCanvasNodeType.STAGE:
            gate_contract = get_default_contract((node.config or {}).get("contract_key"))
            if gate_contract and gate_contract.review_gate:
                return pause_at_node(nid, node.label)

    # Done: clear the resume snapshot so a future re-run of this canvas starts fresh.
    fin = db.session.get(AgentRun, ctx.run_id)
    if fin:
        fc = fin.get_config()
        if fc.pop("_canvas_state", None) is not None:
            fin.set_config(fc)
    canvas.last_run_id = ctx.run_id
    db.session.commit()
    status = AgentRunStatus.PARTIAL if failed else AgentRunStatus.COMPLETED
    return {"status": status, "resource_id": project_id, "extra_credits": extra_credits}
