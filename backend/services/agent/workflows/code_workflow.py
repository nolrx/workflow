"""
Code domain workflow — ``code_full_generation`` (human-in-the-loop, resumable).

Wraps the existing synchronous ``CodeGenerationService`` pipeline (requirements
-> development flow -> document split -> style -> UI previews -> publish) into an
observable agent swarm run. Each stage becomes a recorded step that emits live
events, captures the real prompt/response via the service ``on_model_call`` hook,
and writes its output as an artifact. The final business state is still written
back to the normal ``CodeProject`` / ``CodeDocument`` tables.

**Human-in-the-loop**: a stage in ``REVIEW_STAGES`` pauses the run after it
produces its document — the run goes ``PAUSED`` and the worker exits. The user
then either approves (resume → advance to the next stage) or submits an
adjustment instruction (resume → regenerate that document, fold the instruction
into the ledger, pause again). This makes the workflow **resumable**: every
launch rebuilds its state from the persisted progress cursor + ledger + project,
so it can stop after any reviewed stage and continue later — surviving a closed
page or a server restart.

A per-run **context ledger** (backend/services/agent/context_ledger.py) is seeded
at the planner step, injected into every downstream prompt, enriched after each
step, and verified — deterministically every step plus one AI consistency gate at
the document-split boundary — so later agents stay on-口径 instead of drifting.
User adjustments at a review gate are recorded into the ledger too, so they carry
forward into later生成. The ledger is internal / debug-only and never part of
user-facing output. See docs/agent-context-ledger.md.
"""
import logging
import re
import time

from backend.extensions import db
from backend.models.agent import (
    AgentArtifactType,
    AgentEventLevel,
    AgentEventType,
    AgentRun,
    AgentRunStatus,
    AgentStep,
    AgentStepStatus,
)
from backend.models.code import CodeDocument, CodeProject, CodeProjectStatus, CodeStage
from backend.services import pricing
from backend.services.agent.context_ledger import ContextLedger, seed_from_inputs
from backend.services.agent.context_verifier import (
    emit_context_events,
    gate_available,
    run_ai_consistency_gate,
    run_deterministic_checks,
)
from backend.services.agent.files import artifact_abs_path
from backend.services.code import get_code_generation_service
from backend.services.code.styles import get_styles
from backend.services.code.version_service import safe_record_stage_version
from backend.services.credit_service import charge

logger = logging.getLogger(__name__)

TOTAL_STEPS = 7

# Ordered stages that follow the implicit planner. The planner (project creation
# + ledger seed) always runs first on a fresh run; ``_TAIL`` is what a resume
# advances through.
_TAIL = ["flow", "documents", "style", "preview", "publisher"]
_STAGE_AFTER = {
    "requirements": "flow",
    "flow": "documents",
    "documents": "style",
    # ``style_select`` is a user-input gate (no agent step): on resume it advances
    # into the ``style`` generation stage with the just-picked styles.
    "style_select": "style",
    "style": "preview",
    "preview": "publisher",
    "publisher": None,
}

# Stages that pause for user confirmation before advancing. Every produced
# document is reviewed: the user approves it (advance) or submits an adjustment
# (regenerate + fold into the ledger, pause again). The scheduler (fresh path,
# revise dispatch and _run_from) honours this set generically.
REVIEW_STAGES = {"requirements", "flow", "documents", "style"}

# Per-stage prompt shown when the run pauses for that stage's review.
_PAUSE_MESSAGE = {
    "requirements": "需求文档已生成，请确认或提出调整意见",
    "flow": "开发流程已生成，请确认或提出调整意见",
    "documents": "开发文档已拆分，请确认或提出调整意见",
    # style_select is a selection gate (no document produced yet): the user picks
    # UI styles, then confirming generates the style document.
    "style_select": "请选择 UI 风格，确认后生成风格文档",
    "style": "风格文档已生成，请确认或提出调整意见",
}

# The baseline document types the split step must cover (mirrors the切分原则 in
# document_split_prompt.txt). Used by the deterministic coverage check.
_BASELINE_DOC_TYPES = [
    "product_spec",
    "frontend_spec",
    "backend_spec",
    "data_model",
    "prompt_spec",
    "acceptance_plan",
]


# --- lightweight markdown extraction (best-effort ledger enrichment) ----------
def _md_sections(markdown: str) -> dict:
    """Split markdown into {header_text: body_text} keyed by ## / ### / #### headers."""
    sections: dict = {}
    current = None
    buf: list = []
    for line in (markdown or "").splitlines():
        match = re.match(r"^#{2,4}\s+(.*)", line.strip())
        if match:
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = match.group(1).strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


def _section_body(sections: dict, *keywords: str) -> str:
    """Return the body of the first section whose header contains any keyword."""
    for header, body in sections.items():
        if any(k in header for k in keywords):
            return body
    return ""


def _bullets(text: str, limit: int = 6) -> list:
    """Pull up to ``limit`` non-empty, bullet/number-stripped lines from ``text``."""
    out: list = []
    for line in (text or "").splitlines():
        stripped = line.strip().lstrip("-*•").strip()
        stripped = re.sub(r"^\d+[.、)]\s*", "", stripped).strip()
        if stripped:
            out.append(stripped[:120])
        if len(out) >= limit:
            break
    return out


_REQ_ID_RE = re.compile(r"^(FR|NFR)\s*0*(\d+)", re.IGNORECASE)


def _req_items_from_section(body: str, limit: int = 24) -> list:
    """Pull {id, kind, statement} FR/NFR entries from a 功能范围 / 非功能要求 body.

    Per the requirements prompt each item starts with an FRn / NFRn id; lines
    that don't are skipped. Drives the ledger requirement registry so downstream
    stages reference stable ids instead of re-describing scope.
    """
    out: list = []
    for line in (body or "").splitlines():
        s = line.strip().lstrip("-*•").strip()
        s = re.sub(r"^\d+[.、)]\s*", "", s).strip()
        s = s.replace("**", "").replace("`", "").strip()
        m = _REQ_ID_RE.match(s)
        if not m:
            continue
        kind = m.group(1).upper()
        rid = f"{kind}{int(m.group(2))}"
        statement = s[m.end():].lstrip(" :：.、)-").strip()
        if statement:
            out.append({"id": rid, "kind": kind, "statement": statement[:160]})
        if len(out) >= limit:
            break
    return out


def _await_artifact_on_disk(artifact, *, attempts: int = 10, interval: float = 0.2) -> bool:
    """Block until an artifact's on-disk file is present, non-empty and readable.

    The preview step must not (re)generate the next thumbnail until the current
    one has fully landed on disk. ``save_artifact_file`` writes synchronously, so
    this normally passes on the first probe; the short poll is a safety net
    against slow / networked storage. Best-effort — returns True once the bytes
    are confirmed loadable, False if they never showed up, and never raises.
    """
    if not artifact or not artifact.storage_path:
        return False
    path = artifact_abs_path(artifact.storage_path)
    for _ in range(max(1, attempts)):
        try:
            if path.exists() and path.stat().st_size > 0:
                # Touch the first byte to confirm it is actually loadable, not
                # just a present-but-empty placeholder.
                with open(path, "rb") as handle:
                    if handle.read(1):
                        return True
        except OSError:
            pass
        time.sleep(interval)
    logger.warning("preview artifact %s not confirmed on disk at %s", artifact.id, path)
    return False


def run_code_workflow(ctx, recorder) -> dict:
    """Execute the Code generation pipeline as a resumable, review-gated run.

    Launch paths (decided by the run config's one-shot ``_resume`` directive):
      • fresh  → planner + requirements, then pause at the requirements review gate
      • revise → regenerate the under-review document from the user instruction,
                 fold it into the ledger, pause again
      • approve→ advance from the persisted cursor through the remaining stages
    """
    service = get_code_generation_service()
    config = dict(ctx.config or {})
    resume = config.get("_resume") or None

    run = db.session.get(AgentRun, ctx.run_id)
    prev_progress = run.get_progress() if run else {}
    completed = int(prev_progress.get("completed_steps", 0))
    failed = int(prev_progress.get("failed_steps", 0))
    extra_credits = 0  # per-call context-verify gate charges, surfaced to runtime
    cursor = prev_progress.get("cursor")  # next stage to run on approve

    # Restore the ledger (resume) or start empty (reseeded at the planner step).
    ledger = ContextLedger.load(run.get_context_ledger()) if run else ContextLedger.empty()

    # Resolve inputs from the run config, falling back to the bound project.
    existing = None
    resource_id = ctx.resource_id or (run.resource_id if run else None)
    if resource_id:
        existing = CodeProject.query.filter_by(id=resource_id, user_id=ctx.user_id).first()
    requirement = (
        config.get("requirement") or (existing.requirement_input if existing else "") or ""
    ).strip()
    title = (
        config.get("title") or (existing.title if existing else "") or requirement[:60]
    ).strip()
    style_ids = (
        config.get("style_ids")
        or (existing.get_selected_style_ids() if existing else [])
        or []
    )
    want_previews = bool(config.get("generate_previews", True))
    project_id = existing.id if existing else None

    # --- shared helpers ------------------------------------------------------
    def progress(current_step: str, *, cursor_stage="__keep__", review_stage="__keep__") -> None:
        shown = min(completed, TOTAL_STEPS)
        r = db.session.get(AgentRun, ctx.run_id)
        if r:
            prog = r.get_progress()
            prog["total_steps"] = TOTAL_STEPS
            prog["completed_steps"] = shown
            prog["failed_steps"] = failed
            prog["current_step"] = current_step
            if cursor_stage != "__keep__":
                prog["cursor"] = cursor_stage
            if review_stage != "__keep__":
                prog["review_stage"] = review_stage
            r.set_progress(prog)
            db.session.commit()
        recorder.emit(
            AgentEventType.PROGRESS,
            message=f"进度 {shown}/{TOTAL_STEPS}",
            payload={"completed": shown, "total": TOTAL_STEPS, "current": current_step},
        )

    def persist_ledger() -> None:
        """Persist the current ledger onto the run (the canonical, latest state)."""
        r = db.session.get(AgentRun, ctx.run_id)
        if r:
            r.set_context_ledger(ledger.to_dict())
            db.session.commit()

    def cancel_result() -> dict:
        recorder.emit(
            AgentEventType.WARNING,
            level=AgentEventLevel.WARNING,
            message="收到取消请求，停止后续步骤",
        )
        return {
            "status": AgentRunStatus.CANCELLED,
            "resource_id": project_id,
            "extra_credits": extra_credits,
        }

    def pause_at(stage: str) -> dict:
        """Mark the run paused for review of ``stage`` and end the worker."""
        r = db.session.get(AgentRun, ctx.run_id)
        if r:
            prog = r.get_progress()
            prog["total_steps"] = TOTAL_STEPS
            prog["completed_steps"] = min(completed, TOTAL_STEPS)
            prog["failed_steps"] = failed
            prog["current_step"] = stage
            prog["review_stage"] = stage
            prog["cursor"] = _STAGE_AFTER.get(stage)  # where approve resumes
            r.set_progress(prog)
            db.session.commit()
        recorder.emit(
            AgentEventType.STEP_AWAITING_REVIEW,
            message=_PAUSE_MESSAGE.get(stage, "文档已生成，请确认或提出调整意见"),
            payload={"stage": stage},
        )
        return {
            "status": AgentRunStatus.PAUSED,
            "resource_id": project_id,
            "extra_credits": extra_credits,
        }

    def _attempt_for(agent_key: str) -> int:
        """Attempt number for a new step (counts existing same-key steps incl. self)."""
        return AgentStep.query.filter_by(run_id=ctx.run_id, agent_key=agent_key).count()

    # The seven pipeline stages that count toward progress (planner + the six that
    # follow). Used to recompute ``completed`` from reality on a retry, so re-running
    # a stage that already has a completed step (e.g. a revise that later failed)
    # never drifts the counter.
    _COUNTED_STAGES = {
        "planner", "requirements", "flow", "documents", "style", "preview", "publisher",
    }

    def _completed_count() -> int:
        """Distinct counted stages that currently have a completed step."""
        done = {
            row.agent_key
            for row in AgentStep.query.filter_by(
                run_id=ctx.run_id, status=AgentStepStatus.COMPLETED
            ).all()
        }
        return len(done & _COUNTED_STAGES)

    # --- stage implementations ----------------------------------------------
    def _do_planner() -> None:
        nonlocal project_id, ledger
        with recorder.step(
            "planner", "规划 Agent", "planner", 1, input_summary=f"需求：{requirement[:200]}"
        ) as step:
            if not requirement:
                raise ValueError("需求为空，无法启动 Code 工作流")
            if existing is None:
                proj = CodeProject(
                    user_id=ctx.user_id,
                    team_id=ctx.team_id,
                    title=title,
                    requirement_input=requirement,
                    status=CodeProjectStatus.REQUIREMENT_READY,
                )
                db.session.add(proj)
                db.session.commit()
            else:
                proj = db.session.get(CodeProject, existing.id)
                proj.requirement_input = requirement
                if title:
                    proj.title = title
                db.session.commit()
            project_id = proj.id
            ledger = seed_from_inputs(requirement, proj.title, style_ids)
            plan = {
                "project_id": project_id,
                "target_documents": [
                    "需求文档",
                    "开发流程",
                    "拆分开发文档",
                    "风格文档",
                    "UI 预览缩略图",
                ],
                "selected_styles": style_ids or ["(未选择，默认 minimal-saas)"],
                "generate_previews": want_previews,
                "review_gates": sorted(REVIEW_STAGES),
            }
            step.add_artifact(
                AgentArtifactType.JSON, "执行计划", content_json=plan, filename="plan.json"
            )
            step.set_context(snapshot={"injected_text": "", "ledger": ledger.to_dict()})
            step.set_output(
                output_summary=f"已就绪项目「{proj.title}」，规划 {TOTAL_STEPS} 个步骤。",
                reasoning_summary="先固化需求与目标产物，并初始化上下文共识账本，确保后续每一步口径一致。",
                decision_notes=(
                    f"风格：{style_ids or '默认 minimal-saas'}；"
                    f"预览图：{'开启' if want_previews else '关闭'}；"
                    f"每个文档生成后需用户确认。"
                ),
                self_check="需求非空、项目已创建/定位、账本已播种。",
                next_action="生成需求文档，交用户确认。",
            )
        # Bind the run to the resolved project + persist the seeded ledger.
        r = db.session.get(AgentRun, ctx.run_id)
        r.resource_type = "code_project"
        r.resource_id = project_id
        if not r.title:
            r.title = title
        r.set_context_ledger(ledger.to_dict())
        db.session.commit()

    def _do_requirements(*, revise: bool, instruction: str = "") -> None:
        label = "需求修订 Agent" if revise else "需求 Agent"
        input_summary = (
            f"按调整意见修订需求：{instruction[:300]}" if revise else requirement[:500]
        )
        with recorder.step("requirements", label, "generator", 2, input_summary=input_summary) as step:
            step.step.attempt = _attempt_for("requirements")
            db.session.commit()
            injected = ledger.render_for_prompt()
            proj = db.session.get(CodeProject, project_id)
            if revise:
                doc = service.revise_requirements(
                    proj.requirements_doc or "",
                    instruction,
                    on_delta=step.model_delta_tracer(),
                    on_model_call=step.model_tracer(),
                    context_ledger=injected,
                )
            else:
                doc = service.stream_requirements(
                    requirement,
                    on_delta=step.model_delta_tracer(),
                    on_model_call=step.model_tracer(),
                    context_ledger=injected,
                )
            proj = db.session.get(CodeProject, project_id)
            proj.requirements_doc = doc
            proj.status = CodeProjectStatus.REQUIREMENT_READY
            db.session.commit()
            # Snapshot this product as a new (current) version of the stage.
            safe_record_stage_version(
                proj,
                CodeStage.REQUIREMENTS,
                run_id=ctx.run_id,
                step_id=step.id,
                note=(f"按调整意见修订：{instruction[:200]}" if revise else None),
            )
            step.add_artifact(
                AgentArtifactType.MARKDOWN,
                "需求文档（已修订）" if revise else "需求文档",
                content_text=doc,
                filename="requirements.md",
                mime_type="text/markdown",
                write_file=True,
                domain_ref_type="code_project",
                domain_ref_id=project_id,
            )
            # Establish / refresh the baseline口径 from the requirements doc.
            sections = _md_sections(doc)
            one_liner = _bullets(_section_body(sections, "产品定位", "定位"), 1)
            req_items = (
                _req_items_from_section(_section_body(sections, "功能范围"))
                + _req_items_from_section(_section_body(sections, "非功能"))
            )
            ledger.merge(
                project={
                    "one_liner": one_liner[0] if one_liner else "",
                    "target_users": _bullets(_section_body(sections, "目标用户"), 5),
                    "scope_in": _bullets(_section_body(sections, "功能范围", "功能"), 8),
                },
                requirements_add=req_items,
                # Carry the project-appropriate architecture (designed by the
                # requirements step) forward as consensus constraints so flow /
                # documents / style build on it instead of re-deciding the stack.
                # Kept in tech_stack.constraints (not frontend/backend/data) on
                # purpose: it shapes downstream design口径 without entering the
                # fingerprint, so the frontend-build consistency gate — whose
                # runnable preview is always single-file HTML — is never tripped.
                tech_stack={"constraints": _bullets(_section_body(sections, "技术架构", "架构"), 6)},
                open_questions=_bullets(_section_body(sections, "待确认", "边界"), 6),
                provenance_entry={
                    "step": "requirements_revision" if revise else "requirements",
                    "agent_key": "requirements",
                    "fields_touched": ["project", "requirements", "tech_stack.constraints", "open_questions"],
                },
            )
            det = run_deterministic_checks(
                step_key="requirements",
                ledger=ledger,
                new_output={"text": doc},
                expectations={"nonempty_output": True},
            )
            emit_context_events(
                recorder, step, det_result=det, ai_result=None,
                ledger_after=ledger, injected_text=injected,
            )
            # Generate the clarification questionnaire that drives the front-end
            # quick-confirm dialog at this review gate. It is delivered purely as a
            # JSON artifact on this step (no new CodeProject column, so no schema
            # migration) — the front-end reads it from the run snapshot. Best-effort:
            # a questionnaire hiccup must never fail the (already persisted) doc. Runs
            # for both fresh and revised docs so each round reflects what is still
            # open. Uses no model tracer so it doesn't overwrite the step's doc
            # prompt/response.
            clarify_count = 0
            try:
                questions = service.generate_clarifications(
                    requirement, doc, context_ledger=injected
                )
                clarify_count = len(questions)
                if questions:
                    step.add_artifact(
                        AgentArtifactType.JSON,
                        "需求澄清问卷",
                        content_json={"questions": questions},
                        filename="requirements_questions.json",
                        domain_ref_type="code_project",
                        domain_ref_id=project_id,
                    )
                recorder.emit(
                    AgentEventType.PROGRESS,
                    step_id=step.id,
                    message=f"生成 {clarify_count} 个需求澄清问题，供用户快速确认",
                    payload={"stage": "requirements", "clarify_count": clarify_count},
                )
            except Exception:  # noqa: BLE001 — questionnaire is auxiliary, never fatal
                logger.warning("requirements clarification generation failed", exc_info=True)
                db.session.rollback()
            step.set_output(
                output_summary="需求文档已根据调整重新生成，请再次确认。" if revise
                else "需求文档已生成，请确认或提出调整意见。",
                reasoning_summary="把需求展开为产品定位、目标用户、核心场景、功能范围、贴合该项目的技术架构与待确认问题；技术架构为**推荐方向**，作为待确认约束暂存账本以保持下游口径一致，待澄清问卷确认或采用默认后再固化（高杠杆项不当作已定论）。",
                self_check=f"文档长度约 {len(doc)} 字符；生成 {clarify_count} 个澄清问题。",
                next_action="等待用户确认；确认后生成开发流程。",
            )

    def _do_flow(*, revise: bool = False, instruction: str = "") -> None:
        nonlocal extra_credits
        label = "开发流程修订 Agent" if revise else "开发流程 Agent"
        input_summary = (
            f"按调整意见修订流程：{instruction[:300]}" if revise else "基于需求文档生成开发流程"
        )
        with recorder.step("flow", label, "generator", 3, input_summary=input_summary) as step:
            step.step.attempt = _attempt_for("flow")
            db.session.commit()
            proj = db.session.get(CodeProject, project_id)
            injected = ledger.render_for_prompt()
            if revise:
                flow = service.revise_development_flow(
                    proj.development_flow or "",
                    instruction,
                    on_delta=step.model_delta_tracer(),
                    on_model_call=step.model_tracer(),
                    context_ledger=injected,
                )
            else:
                flow = service.stream_development_flow(
                    proj.requirements_doc,
                    on_delta=step.model_delta_tracer(),
                    on_model_call=step.model_tracer(),
                    context_ledger=injected,
                )
            proj = db.session.get(CodeProject, project_id)
            proj.development_flow = flow
            proj.status = CodeProjectStatus.FLOW_READY
            db.session.commit()
            safe_record_stage_version(
                proj, CodeStage.FLOW, run_id=ctx.run_id, step_id=step.id,
                note=(f"按调整意见修订：{instruction[:200]}" if revise else None),
            )
            step.add_artifact(
                AgentArtifactType.MARKDOWN,
                "开发流程",
                content_text=flow,
                filename="development_flow.md",
                mime_type="text/markdown",
                write_file=True,
                domain_ref_type="code_project",
                domain_ref_id=project_id,
            )
            sections = _md_sections(flow)
            ledger.merge(
                tech_stack={"constraints": _bullets(_section_body(sections, "技术假设", "技术"), 6)},
                provenance_entry={
                    "step": "flow_revision" if revise else "flow",
                    "agent_key": "flow",
                    "fields_touched": ["tech_stack.constraints"],
                },
            )
            det = run_deterministic_checks(
                step_key="flow",
                ledger=ledger,
                new_output={"text": flow},
                expectations={"nonempty_output": True, "required_ledger_fields": ["project.one_liner"]},
            )
            # AI consistency gate at the requirements -> flow boundary: the dev
            # flow must not silently re-choose the tech stack or drop an FR/NFR
            # established by the requirements doc. Charged per call; skipped when
            # no text provider is configured.
            ai_result = None
            if not gate_available():
                recorder.emit(
                    AgentEventType.PROGRESS,
                    step_id=step.id,
                    message="未配置文本模型，跳过上下文一致性 AI 闸门（仅程序化校验）",
                )
            elif charge(
                user_id=ctx.user_id,
                amount=pricing.CODE_CONTEXT_VERIFY,
                operation="code_context_verify",
                resource_type="agent_run",
                resource_id=ctx.run_id,
                description="context verify @ flow",
                team_id=ctx.team_id,
            ):
                extra_credits += pricing.CODE_CONTEXT_VERIFY
                ai_result = run_ai_consistency_gate(
                    # 8000 (was 2000): the adversarial consistency gate must see
                    # most of the flow doc — a 2000-char window dropped the later
                    # sections (后端服务 / AI链路 / 里程碑) where drift often hides.
                    ledger=ledger, new_product_summary=flow[:8000], step_key="flow"
                )
            else:
                recorder.emit(
                    AgentEventType.WARNING,
                    level=AgentEventLevel.WARNING,
                    step_id=step.id,
                    message="积分不足，本步仅执行程序化上下文校验",
                )
            emit_context_events(
                recorder, step, det_result=det, ai_result=ai_result,
                ledger_after=ledger, injected_text=injected,
            )
            step.set_output(
                output_summary="开发流程已根据调整重新生成，请再次确认。" if revise
                else "开发流程文档已生成，请确认或提出调整意见。",
                reasoning_summary="将需求拆解为技术假设、模块划分、里程碑与验收标准，并把技术假设并入账本约束。",
                next_action="等待用户确认；确认后拆分开发文档。",
            )

    def _do_documents(*, revise: bool = False, instruction: str = "") -> None:
        nonlocal extra_credits
        label = "文档拆分修订 Agent" if revise else "文档拆分 Agent"
        input_summary = (
            f"按调整意见修订文档：{instruction[:300]}" if revise else "基于需求与流程拆分开发文档"
        )
        with recorder.step("documents", label, "generator", 4, input_summary=input_summary) as step:
            step.step.attempt = _attempt_for("documents")
            db.session.commit()
            proj = db.session.get(CodeProject, project_id)
            injected = ledger.render_for_prompt()
            if revise:
                current_docs = [
                    {
                        "document_type": d.document_type,
                        "title": d.title,
                        "content": d.content,
                        "prompt_expert": d.prompt_expert,
                        "order_index": d.order_index,
                    }
                    for d in proj.documents.all()
                ]
                docs = service.revise_documents(
                    proj.requirements_doc,
                    proj.development_flow,
                    current_docs,
                    instruction,
                    on_delta=step.model_delta_tracer(),
                    on_model_call=step.model_tracer(),
                    context_ledger=injected,
                )
            else:
                docs = service.stream_documents(
                    proj.requirements_doc,
                    proj.development_flow,
                    on_delta=step.model_delta_tracer(),
                    on_model_call=step.model_tracer(),
                    context_ledger=injected,
                )
            # NB: project.documents has order_by, so .delete() on it raises under
            # SQLAlchemy 2.x; delete via a plain query instead.
            CodeDocument.query.filter_by(project_id=project_id).delete()
            created = []
            for item in docs:
                document = CodeDocument(
                    project_id=project_id,
                    document_type=item["document_type"],
                    title=item["title"],
                    content=item["content"],
                    prompt_expert=item["prompt_expert"],
                    order_index=item["order_index"],
                )
                db.session.add(document)
                created.append(document)
            proj = db.session.get(CodeProject, project_id)
            proj.status = CodeProjectStatus.DOCUMENTS_READY
            db.session.commit()
            safe_record_stage_version(
                proj, CodeStage.DOCUMENTS, run_id=ctx.run_id, step_id=step.id
            )
            for document, item in zip(created, docs):
                step.add_artifact(
                    AgentArtifactType.MARKDOWN,
                    document.title,
                    content_text=item["content"],
                    filename=f"{item['document_type']}.md",
                    mime_type="text/markdown",
                    write_file=True,
                    domain_ref_type="code_document",
                    domain_ref_id=document.id,
                )
            ledger.merge(
                glossary_add=[
                    {
                        "term": item["title"],
                        "definition": f"{item['document_type']} 开发文档",
                        "source_step": "documents",
                    }
                    for item in docs
                ],
                provenance_entry={
                    "step": "documents_revision" if revise else "documents",
                    "agent_key": "documents",
                    "fields_touched": ["glossary"],
                },
            )
            det = run_deterministic_checks(
                step_key="documents",
                ledger=ledger,
                new_output={
                    "text": "\n".join(i["content"] for i in docs),
                    "doc_types": [i["document_type"] for i in docs],
                },
                expectations={"nonempty_output": True, "doc_types_covered": _BASELINE_DOC_TYPES},
            )
            # AI consistency gate at this high-risk boundary. Charged per call;
            # never charged when no provider is configured.
            ai_result = None
            if not gate_available():
                recorder.emit(
                    AgentEventType.PROGRESS,
                    step_id=step.id,
                    message="未配置文本模型，跳过上下文一致性 AI 闸门（仅程序化校验）",
                )
            elif charge(
                user_id=ctx.user_id,
                amount=pricing.CODE_CONTEXT_VERIFY,
                operation="code_context_verify",
                resource_type="agent_run",
                resource_id=ctx.run_id,
                description="context verify @ documents",
                team_id=ctx.team_id,
            ):
                extra_credits += pricing.CODE_CONTEXT_VERIFY
                summary = "\n".join(
                    f"- {i['title']} ({i['document_type']}): {i['content'][:200]}" for i in docs
                )
                ai_result = run_ai_consistency_gate(
                    ledger=ledger, new_product_summary=summary, step_key="documents"
                )
            else:
                recorder.emit(
                    AgentEventType.WARNING,
                    level=AgentEventLevel.WARNING,
                    step_id=step.id,
                    message="积分不足，本步仅执行程序化上下文校验",
                )
            emit_context_events(
                recorder, step, det_result=det, ai_result=ai_result,
                ledger_after=ledger, injected_text=injected,
            )
            step.set_output(
                output_summary=(
                    f"已按调整重新拆分 {len(created)} 份开发文档，请再次确认。" if revise
                    else f"已拆分 {len(created)} 份可编辑开发文档，请确认或提出调整意见。"
                ),
                reasoning_summary="按产品/开发/前端/后端/提示词/验收等维度切分，每份附提示词专家建议，并对照账本做一致性校验。",
                self_check=f"生成 {len(created)} 份文档；{det['summary']}。",
                next_action="等待用户确认；确认后生成风格文档。",
            )

    def _do_style(*, revise: bool = False, instruction: str = "") -> None:
        label = "风格修订 Agent" if revise else "风格 Agent"
        input_summary = (
            f"按调整意见修订风格：{instruction[:300]}" if revise
            else f"风格选择：{style_ids or '默认 minimal-saas'}"
        )
        with recorder.step("style", label, "generator", 5, input_summary=input_summary) as step:
            step.step.attempt = _attempt_for("style")
            db.session.commit()
            proj = db.session.get(CodeProject, project_id)
            # Prefer the styles the user picked at the style_select gate (persisted
            # on the project); fall back to the run-start config / default.
            chosen_styles = proj.get_selected_style_ids() or style_ids or ["minimal-saas"]
            injected = ledger.render_for_prompt()
            if revise:
                style_doc = service.revise_style_prompt(
                    proj.style_prompt or "",
                    instruction,
                    on_delta=step.model_delta_tracer(),
                    on_model_call=step.model_tracer(),
                    context_ledger=injected,
                )
            else:
                style_doc = service.stream_style_prompt(
                    proj.requirement_input,
                    chosen_styles,
                    on_delta=step.model_delta_tracer(),
                    on_model_call=step.model_tracer(),
                    context_ledger=injected,
                )
            proj = db.session.get(CodeProject, project_id)
            proj.set_selected_style_ids(chosen_styles)
            proj.style_prompt = style_doc
            proj.status = CodeProjectStatus.STYLE_READY
            db.session.commit()
            safe_record_stage_version(
                proj, CodeStage.STYLE, run_id=ctx.run_id, step_id=step.id,
                note=(f"按调整意见修订：{instruction[:200]}" if revise else None),
            )
            step.add_artifact(
                AgentArtifactType.MARKDOWN,
                "风格文档",
                content_text=style_doc,
                filename="style.md",
                mime_type="text/markdown",
                write_file=True,
                domain_ref_type="code_project",
                domain_ref_id=project_id,
            )
            sections = _md_sections(style_doc)
            tone = _bullets(_section_body(sections, "UI 基调", "基调", "视觉定位"), 1)
            ledger.merge(
                decisions_add=[
                    {
                        "id": "ui-tone",
                        "statement": tone[0] if tone else "已确立 UI 风格基调",
                        "rationale": "风格 Agent 产出，供前端代码生成遵循",
                        "source_step": "style",
                    }
                ],
                constraints_add=[f"UI 风格: {', '.join(str(s) for s in chosen_styles)}"],
                provenance_entry={
                    "step": "style_revision" if revise else "style",
                    "agent_key": "style",
                    "fields_touched": ["decisions", "constraints"],
                },
            )
            det = run_deterministic_checks(
                step_key="style",
                ledger=ledger,
                new_output={"text": style_doc},
                expectations={"nonempty_output": True},
            )
            emit_context_events(
                recorder, step, det_result=det, ai_result=None,
                ledger_after=ledger, injected_text=injected,
            )
            step.set_output(
                output_summary="风格文档已根据调整重新生成，请再次确认。" if revise
                else "风格文档已生成，请确认或提出调整意见。",
                decision_notes=(
                    "使用用户所选风格。" if style_ids else "用户未选风格，默认采用 minimal-saas。"
                ),
                next_action="等待用户确认；确认后生成 UI 预览缩略图。",
            )

    def _do_preview() -> bool:
        nonlocal failed
        preview_ok = True
        with recorder.step(
            "preview", "预览图 Agent", "generator", 6, input_summary="基于风格文档生成 UI 预览缩略图"
        ) as step:
            proj = db.session.get(CodeProject, project_id)
            prompt = (proj.style_prompt or "").strip()
            # Reuse existing thumbnails when the selected styles are unchanged: each
            # thumbnail is stamped with the style_ids it was generated for, so we
            # only re-spend an image-API call when the user actually picked a
            # different style. (No stamp / different ids / no thumbnails → regenerate.)
            existing_previews = proj.get_preview_images()
            current_style_ids = set(proj.get_selected_style_ids())
            existing_style_ids = (
                set(existing_previews[0].get("style_ids", [])) if existing_previews else set()
            )
            previews_reusable = bool(
                existing_previews and current_style_ids and existing_style_ids == current_style_ids
            )
            if not want_previews:
                step.mark_skipped("配置未开启预览图生成")
            elif not prompt:
                step.mark_skipped("缺少风格提示词，跳过预览图")
            elif previews_reusable:
                # Style unchanged + thumbnails already exist → reuse as-is. Do NOT
                # regenerate, record a new stage version, or reset status (keeps any
                # prior ui_confirmed so the user goes straight to project generation).
                recorder.emit(
                    AgentEventType.MODEL_RESPONSE,
                    step_id=step.id,
                    message=f"风格未变更，复用已有 {len(existing_previews)} 张 UI 预览缩略图",
                    payload={"count": len(existing_previews), "reused": True},
                )
                step.set_output(
                    output_summary=f"风格未变更，复用现有 {len(existing_previews)} 张 UI 预览缩略图。",
                    next_action="在 Code 工作台选择并确认 UI 基调。",
                )
            else:
                recorder.emit(
                    AgentEventType.MODEL_REQUEST,
                    step_id=step.id,
                    message="请求生成 UI 预览图",
                    payload={"prompt": prompt},
                )
                def _persist_preview(index: int, image: dict, image_bytes: bytes) -> None:
                    """Write one thumbnail to disk + a compact file URL.

                    The raw PNG goes to disk (``storage_path``) and ``preview_url``
                    holds only the short ``/file`` route — NOT the ~1.7MB base64
                    data URL, which overflows preview_url's varchar(1000) and used
                    to abort the whole run. We then block until the file is
                    back-readable, so the generator only advances to the next
                    thumbnail once this one has truly landed on disk.
                    """
                    title = image.get("id") or f"预览图-{index + 1}"
                    artifact = step.add_artifact(
                        AgentArtifactType.IMAGE,
                        title,
                        filename=f"{title}.png",
                        mime_type="image/png",
                        write_file=True,
                        content_bytes=image_bytes,
                        domain_ref_type="code_project",
                        domain_ref_id=project_id,
                    )
                    artifact.preview_url = f"/api/agent/artifacts/{artifact.id}/file"
                    db.session.commit()
                    _await_artifact_on_disk(artifact)

                try:
                    images = service.generate_preview_images(
                        prompt, count=2, on_image=_persist_preview
                    )
                except RuntimeError as error:
                    preview_ok = False
                    failed += 1
                    step.mark_failed(str(error))
                else:
                    proj = db.session.get(CodeProject, project_id)
                    # Stamp each thumbnail with the styles it was generated for so a
                    # later run can reuse them when the style selection is unchanged.
                    stamp_ids = proj.get_selected_style_ids()
                    for image in images:
                        image["style_ids"] = stamp_ids
                    # The project keeps the inline base64 data URLs so the Code
                    # workspace <img> previews render without an auth round-trip;
                    # the disk-backed artifacts (above) are what the run timeline
                    # serves. preview_images_raw is a Text column, so the base64
                    # fits there — only the artifact's varchar preview_url did not.
                    proj.set_preview_images(images)
                    proj.status = CodeProjectStatus.PREVIEW_READY
                    db.session.commit()
                    safe_record_stage_version(
                        proj, CodeStage.PREVIEW, run_id=ctx.run_id, step_id=step.id
                    )
                    recorder.emit(
                        AgentEventType.MODEL_RESPONSE,
                        step_id=step.id,
                        message=f"生成 {len(images)} 张预览图",
                        payload={"count": len(images)},
                    )
                    step.set_output(
                        output_summary=f"已生成 {len(images)} 张 UI 预览缩略图。",
                        next_action="在 Code 工作台选择并确认 UI 基调。",
                    )
            step.set_context(snapshot={"injected_text": "", "ledger": ledger.to_dict()})
        return preview_ok

    def _do_publisher(preview_ok: bool) -> None:
        with recorder.step(
            "publisher", "发布 Agent", "publisher", 7, input_summary="汇总并发布项目状态"
        ) as step:
            proj = db.session.get(CodeProject, project_id)
            step.add_artifact(
                AgentArtifactType.JSON,
                "项目快照",
                content_json=proj.to_dict(include_documents=True),
                filename="project.json",
            )
            step.add_artifact(
                AgentArtifactType.JSON,
                "上下文账本（最终）",
                content_json=ledger.to_dict(),
                filename="context_ledger.json",
                domain_ref_type="code_project",
                domain_ref_id=project_id,
            )
            step.set_context(snapshot={"injected_text": "", "ledger": ledger.to_dict()})
            step.set_output(
                output_summary=f"项目「{proj.title}」已发布，当前状态 {proj.status}。",
                reasoning_summary="所有产物已写回 CodeProject / CodeDocument，上下文账本已固化，可在 Code 工作台继续编辑。",
                self_check=(
                    f"需求/流程/文档({proj.documents.count()})/风格 均已生成；"
                    f"预览图 {'成功' if preview_ok else '失败或跳过'}。"
                ),
                next_action="在 Code 工作台确认 UI 基调。",
            )

    def _run_from(start_stage: str) -> dict:
        """Run the tail stages from ``start_stage`` to the end (pausing at gates)."""
        nonlocal completed
        preview_ok = True
        try:
            start_idx = _TAIL.index(start_stage)
        except ValueError:
            start_idx = 0
        for stage in _TAIL[start_idx:]:
            if ctx.is_cancelled():
                return cancel_result()
            if stage == "flow":
                _do_flow()
                completed += 1
            elif stage == "documents":
                _do_documents()
                completed += 1
            elif stage == "style":
                _do_style()
                completed += 1
            elif stage == "preview":
                preview_ok = _do_preview()
                if preview_ok:
                    completed += 1
            elif stage == "publisher":
                _do_publisher(preview_ok)
                completed += 1
            persist_ledger()
            nxt = _STAGE_AFTER.get(stage)
            progress(nxt or "done", cursor_stage=nxt)
            # Future-proof: pause here if this stage is a review gate.
            if stage in REVIEW_STAGES:
                return pause_at(stage)
        status = AgentRunStatus.COMPLETED if preview_ok else AgentRunStatus.PARTIAL
        return {"status": status, "resource_id": project_id, "extra_credits": extra_credits}

    # --- scheduler -----------------------------------------------------------
    # Consume the one-shot resume directive so a future relaunch never repeats it.
    if resume and run:
        cfg = run.get_config()
        cfg.pop("_resume", None)
        run.set_config(cfg)
        db.session.commit()

    # RETRY: a previous run failed at some stage; re-run from that stage to the end,
    # re-using everything earlier stages already produced (project, documents,
    # ledger). The failed stage is authoritative — read it from the step that ended
    # ``failed`` (latest wins), falling back to the directive hint / progress cursor.
    if resume and resume.get("action") == "retry":
        failed_step = (
            AgentStep.query.filter_by(run_id=ctx.run_id, status=AgentStepStatus.FAILED)
            .order_by(AgentStep.order_index.desc(), AgentStep.created_at.desc())
            .first()
        )
        retry_stage = (
            resume.get("stage")
            or (failed_step.agent_key if failed_step else None)
            or prev_progress.get("current_step")
            or "requirements"
        )
        # The same primitive backs a user-initiated retry and an automatic
        # restart-resume (reconcile_orphaned_runs hands us this directive with
        # reason="service_restart"); narrate them distinctly on the timeline.
        _restarted = resume.get("reason") == "service_restart"
        recorder.emit(
            AgentEventType.REVIEW_RESOLVED,
            message=(
                f"服务重启，从「{retry_stage}」阶段自动续跑"
                if _restarted
                else f"重试「{retry_stage}」阶段"
            ),
            payload={"stage": retry_stage, "retry": True, "resumed": _restarted},
        )
        # planner failed → nothing usable downstream; restart from project creation.
        if retry_stage == "planner":
            _do_planner()
            completed = _completed_count()
            progress("requirements", cursor_stage="requirements", review_stage=None)
            if ctx.is_cancelled():
                return cancel_result()
            _do_requirements(revise=False)
            completed = _completed_count()
            persist_ledger()
            return pause_at("requirements")
        # requirements is the first review stage (handled outside _TAIL).
        if retry_stage == "requirements":
            _do_requirements(revise=False)
            completed = _completed_count()
            persist_ledger()
            return pause_at("requirements")
        # flow / documents / style / preview / publisher all live on the tail. Reset
        # the running counter to reality first so _run_from's per-stage increments
        # start from the true number of completed stages.
        if retry_stage in _TAIL:
            completed = _completed_count()
            return _run_from(retry_stage)
        # Unknown / stale stage → safest is to re-run requirements and re-gate.
        _do_requirements(revise=False)
        completed = _completed_count()
        persist_ledger()
        return pause_at("requirements")

    # REVISE: regenerate the document under review, then pause again.
    if resume and resume.get("action") == "revise":
        stage = resume.get("stage") or "requirements"
        instruction = (resume.get("instruction") or "").strip()
        recorder.emit(
            AgentEventType.USER_REVISION,
            message=f"收到调整意见：{instruction}",
            payload={"stage": stage, "instruction": instruction},
        )
        ledger.record_user_revision(stage, instruction)
        persist_ledger()
        revise_fn = {
            "requirements": _do_requirements,
            "flow": _do_flow,
            "documents": _do_documents,
            "style": _do_style,
        }.get(stage)
        if revise_fn is None:
            raise ValueError(f"stage 不支持调整: {stage}")
        revise_fn(revise=True, instruction=instruction)
        persist_ledger()
        return pause_at(stage)

    # APPROVE: advance from the persisted cursor through the remaining stages.
    # The resume route clears progress.review_stage before relaunch, so the stage
    # being approved is read from the resume directive, not prev_progress.
    if resume and resume.get("action") == "approve":
        approved_stage = resume.get("stage") or prev_progress.get("review_stage")
        recorder.emit(
            AgentEventType.REVIEW_RESOLVED,
            message="已确认，继续后续生成",
            payload={"stage": approved_stage},
        )
        # After the split documents are approved, pause for UI-style selection
        # instead of auto-generating the style document: the user picks styles,
        # and confirming (select_style) generates the style doc from that choice.
        if approved_stage == "documents":
            return pause_at("style_select")
        return _run_from(cursor or "flow")

    # SELECT_STYLE: the user picked UI styles at the style_select gate. Persist the
    # choice, generate the style document from it, then pause at the style review.
    if resume and resume.get("action") == "select_style":
        chosen = [style.id for style in get_styles(resume.get("style_ids") or [])]
        proj = db.session.get(CodeProject, project_id)
        proj.set_selected_style_ids(chosen)
        db.session.commit()
        recorder.emit(
            AgentEventType.REVIEW_RESOLVED,
            message=f"已选择 UI 风格：{chosen or '默认 minimal-saas'}",
            payload={"stage": "style_select", "style_ids": chosen},
        )
        ledger.merge(
            constraints_add=[
                f"UI 风格(用户选定): {', '.join(chosen) if chosen else 'minimal-saas'}"
            ],
            provenance_entry={
                "step": "style_select",
                "agent_key": "style",
                "fields_touched": ["constraints"],
            },
        )
        persist_ledger()
        _do_style(revise=False)
        # Count the style step here (it runs outside _run_from), mirroring the old
        # documents-approve path so progress reaches 7/7 on completion, not 6/7.
        completed += 1
        persist_ledger()
        return pause_at("style")

    # FRESH: planner + requirements, then pause for the first review.
    _do_planner()
    completed += 1
    progress("requirements", cursor_stage="requirements", review_stage=None)
    if ctx.is_cancelled():
        return cancel_result()
    _do_requirements(revise=False)
    completed += 1
    persist_ledger()
    return pause_at("requirements")
