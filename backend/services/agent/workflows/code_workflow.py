"""
Code domain workflow — ``code_full_generation``.

Wraps the existing synchronous ``CodeGenerationService`` pipeline (requirements
-> development flow -> document split -> style -> UI previews -> publish) into an
observable agent swarm run. Each stage becomes a recorded step that emits live
events, captures the real prompt/response via the service ``on_model_call`` hook,
and writes its output as an artifact. The final business state is still written
back to the normal ``CodeProject`` / ``CodeDocument`` tables.

A per-run **context ledger** (backend/services/agent/context_ledger.py) is seeded
at the planner step, injected into every downstream prompt, enriched after each
step, and verified — deterministically every step plus one AI consistency gate at
the document-split boundary — so later agents stay on-口径 instead of drifting.
The ledger is internal / debug-only and never part of user-facing output. See
docs/agent-context-ledger.md.
"""
import logging
import re

from backend.extensions import db
from backend.models.agent import (
    AgentArtifactType,
    AgentEventLevel,
    AgentEventType,
    AgentRun,
    AgentRunStatus,
)
from backend.models.code import CodeDocument, CodeProject, CodeProjectStatus
from backend.services import pricing
from backend.services.agent.context_ledger import ContextLedger, seed_from_inputs
from backend.services.agent.context_verifier import (
    emit_context_events,
    gate_available,
    run_ai_consistency_gate,
    run_deterministic_checks,
)
from backend.services.code import get_code_generation_service
from backend.services.credit_service import charge

logger = logging.getLogger(__name__)

TOTAL_STEPS = 7

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


def run_code_workflow(ctx, recorder) -> dict:
    """Execute the full Code generation pipeline as an agent run."""
    service = get_code_generation_service()
    config = ctx.config or {}

    # Resolve inputs from the run config, falling back to an existing project.
    existing = None
    if ctx.resource_id:
        existing = CodeProject.query.filter_by(
            id=ctx.resource_id, user_id=ctx.user_id
        ).first()

    requirement = (
        config.get("requirement")
        or (existing.requirement_input if existing else "")
        or ""
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

    completed = 0
    failed = 0
    extra_credits = 0  # per-call context-verify gate charges, surfaced to runtime
    project_id = existing.id if existing else None
    ledger = ContextLedger.empty()  # reseeded at the planner step

    def progress(current_step: str) -> None:
        run = db.session.get(AgentRun, ctx.run_id)
        if run:
            run.set_progress(
                {
                    "total_steps": TOTAL_STEPS,
                    "completed_steps": completed,
                    "failed_steps": failed,
                    "current_step": current_step,
                }
            )
            db.session.commit()
        recorder.emit(
            AgentEventType.PROGRESS,
            message=f"进度 {completed}/{TOTAL_STEPS}",
            payload={"completed": completed, "total": TOTAL_STEPS, "current": current_step},
        )

    def persist_ledger() -> None:
        """Persist the current ledger onto the run (the canonical, latest state)."""
        run = db.session.get(AgentRun, ctx.run_id)
        if run:
            run.set_context_ledger(ledger.to_dict())
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

    # --- Step 1: Planner -----------------------------------------------------
    with recorder.step(
        "planner", "规划 Agent", "planner", 1, input_summary=f"需求：{requirement[:200]}"
    ) as step:
        if not requirement:
            raise ValueError("需求为空，无法启动 Code 工作流")

        if existing is None:
            project = CodeProject(
                user_id=ctx.user_id,
                team_id=ctx.team_id,
                title=title,
                requirement_input=requirement,
                status=CodeProjectStatus.REQUIREMENT_READY,
            )
            db.session.add(project)
            db.session.commit()
        else:
            project = db.session.get(CodeProject, existing.id)
            project.requirement_input = requirement
            if title:
                project.title = title
            db.session.commit()
        project_id = project.id

        # Seed the context ledger from the run inputs (project定位 + default stack).
        ledger = seed_from_inputs(requirement, project.title, style_ids)

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
        }
        step.add_artifact(
            AgentArtifactType.JSON, "执行计划", content_json=plan, filename="plan.json"
        )
        step.set_context(snapshot={"injected_text": "", "ledger": ledger.to_dict()})
        step.set_output(
            output_summary=f"已就绪项目「{project.title}」，规划 {TOTAL_STEPS} 个步骤。",
            reasoning_summary="先固化需求与目标产物，并初始化上下文共识账本，确保后续每一步口径一致。",
            decision_notes=(
                f"风格：{style_ids or '默认 minimal-saas'}；"
                f"预览图：{'开启' if want_previews else '关闭'}。"
            ),
            self_check="需求非空、项目已创建/定位、账本已播种。",
            next_action="生成需求文档。",
        )

    # Bind the run to the resolved project + persist the seeded ledger.
    run = db.session.get(AgentRun, ctx.run_id)
    run.resource_type = "code_project"
    run.resource_id = project_id
    if not run.title:
        run.title = title
    run.set_context_ledger(ledger.to_dict())
    db.session.commit()
    completed += 1
    progress("requirements")

    # --- Step 2: Requirements ------------------------------------------------
    if ctx.is_cancelled():
        return cancel_result()
    with recorder.step(
        "requirements", "需求 Agent", "generator", 2, input_summary=requirement[:500]
    ) as step:
        injected = ledger.render_for_prompt()
        doc = service.stream_requirements(
            requirement,
            on_delta=step.model_delta_tracer(),
            on_model_call=step.model_tracer(),
            context_ledger=injected,
        )
        project = db.session.get(CodeProject, project_id)
        project.requirements_doc = doc
        project.status = CodeProjectStatus.REQUIREMENT_READY
        db.session.commit()
        step.add_artifact(
            AgentArtifactType.MARKDOWN,
            "需求文档",
            content_text=doc,
            filename="requirements.md",
            mime_type="text/markdown",
            write_file=True,
            domain_ref_type="code_project",
            domain_ref_id=project_id,
        )
        # Establish the baseline口径 from the requirements doc (best-effort parse).
        sections = _md_sections(doc)
        one_liner = _bullets(_section_body(sections, "产品定位", "定位"), 1)
        ledger.merge(
            project={
                "one_liner": one_liner[0] if one_liner else "",
                "target_users": _bullets(_section_body(sections, "目标用户"), 5),
                "scope_in": _bullets(_section_body(sections, "功能范围", "功能"), 8),
            },
            open_questions=_bullets(_section_body(sections, "待确认", "边界"), 6),
            provenance_entry={
                "step": "requirements",
                "agent_key": "requirements",
                "fields_touched": ["project", "open_questions"],
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
        step.set_output(
            output_summary="需求文档已生成。",
            reasoning_summary="把一句话需求展开为产品定位、目标用户、核心场景、功能范围与待确认问题，并写入共识账本。",
            self_check=f"文档长度约 {len(doc)} 字符。",
            next_action="基于需求文档生成开发流程。",
        )
    persist_ledger()
    completed += 1
    progress("flow")

    # --- Step 3: Development flow --------------------------------------------
    if ctx.is_cancelled():
        return cancel_result()
    with recorder.step(
        "flow", "开发流程 Agent", "generator", 3, input_summary="基于需求文档生成开发流程"
    ) as step:
        project = db.session.get(CodeProject, project_id)
        injected = ledger.render_for_prompt()
        flow = service.stream_development_flow(
            project.requirements_doc,
            on_delta=step.model_delta_tracer(),
            on_model_call=step.model_tracer(),
            context_ledger=injected,
        )
        project = db.session.get(CodeProject, project_id)
        project.development_flow = flow
        project.status = CodeProjectStatus.FLOW_READY
        db.session.commit()
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
                "step": "flow",
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
        emit_context_events(
            recorder, step, det_result=det, ai_result=None,
            ledger_after=ledger, injected_text=injected,
        )
        step.set_output(
            output_summary="开发流程文档已生成。",
            reasoning_summary="将需求拆解为技术假设、模块划分、里程碑与验收标准，并把技术假设并入账本约束。",
            next_action="把需求与流程拆分为可编辑的开发文档。",
        )
    persist_ledger()
    completed += 1
    progress("documents")

    # --- Step 4: Document split (HIGH-RISK BOUNDARY — AI consistency gate) ---
    if ctx.is_cancelled():
        return cancel_result()
    with recorder.step(
        "documents", "文档拆分 Agent", "generator", 4, input_summary="基于需求与流程拆分开发文档"
    ) as step:
        project = db.session.get(CodeProject, project_id)
        injected = ledger.render_for_prompt()
        docs = service.stream_documents(
            project.requirements_doc,
            project.development_flow,
            on_delta=step.model_delta_tracer(),
            on_model_call=step.model_tracer(),
            context_ledger=injected,
        )
        project = db.session.get(CodeProject, project_id)
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
        project.status = CodeProjectStatus.DOCUMENTS_READY
        db.session.commit()
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

        # Enrich the ledger glossary with the produced documents.
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
                "step": "documents",
                "agent_key": "documents",
                "fields_touched": ["glossary"],
            },
        )

        # Deterministic coverage + non-empty checks.
        det = run_deterministic_checks(
            step_key="documents",
            ledger=ledger,
            new_output={
                "text": "\n".join(i["content"] for i in docs),
                "doc_types": [i["document_type"] for i in docs],
            },
            expectations={"nonempty_output": True, "doc_types_covered": _BASELINE_DOC_TYPES},
        )

        # AI consistency gate at this high-risk boundary. Charged per call; never
        # charged when no provider is configured; the model call deliberately does
        # NOT use step.model_tracer() so the split prompt/response trace is kept.
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
            output_summary=f"已拆分 {len(created)} 份可编辑开发文档。",
            reasoning_summary="按产品/开发/前端/后端/提示词/验收等维度切分，每份附提示词专家建议，并对照账本做一致性校验。",
            self_check=f"生成 {len(created)} 份文档；{det['summary']}。",
            next_action="根据所选 UI 风格生成风格文档。",
        )
    persist_ledger()
    completed += 1
    progress("style")

    # --- Step 5: Style -------------------------------------------------------
    if ctx.is_cancelled():
        return cancel_result()
    with recorder.step(
        "style",
        "风格 Agent",
        "generator",
        5,
        input_summary=f"风格选择：{style_ids or '默认 minimal-saas'}",
    ) as step:
        chosen_styles = style_ids or ["minimal-saas"]
        project = db.session.get(CodeProject, project_id)
        injected = ledger.render_for_prompt()
        style_doc = service.stream_style_prompt(
            project.requirement_input,
            chosen_styles,
            on_delta=step.model_delta_tracer(),
            on_model_call=step.model_tracer(),
            context_ledger=injected,
        )
        project = db.session.get(CodeProject, project_id)
        project.set_selected_style_ids(chosen_styles)
        project.style_prompt = style_doc
        project.status = CodeProjectStatus.STYLE_READY
        db.session.commit()
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
                "step": "style",
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
            output_summary="风格文档已生成。",
            decision_notes=(
                "使用用户所选风格。" if style_ids else "用户未选风格，默认采用 minimal-saas。"
            ),
            next_action="生成 UI 预览缩略图。",
        )
    persist_ledger()
    completed += 1
    progress("preview")

    # --- Step 6: Preview thumbnails (non-critical) ---------------------------
    if ctx.is_cancelled():
        return cancel_result()
    preview_ok = True
    with recorder.step(
        "preview", "预览图 Agent", "generator", 6, input_summary="基于风格文档生成 UI 预览缩略图"
    ) as step:
        project = db.session.get(CodeProject, project_id)
        prompt = (project.style_prompt or "").strip()
        if not want_previews:
            step.mark_skipped("配置未开启预览图生成")
        elif not prompt:
            step.mark_skipped("缺少风格提示词，跳过预览图")
        else:
            recorder.emit(
                AgentEventType.MODEL_REQUEST,
                step_id=step.id,
                message="请求生成 UI 预览图",
                payload={"prompt": prompt},
            )
            try:
                images = service.generate_preview_images(prompt, count=2)
            except RuntimeError as error:
                preview_ok = False
                failed += 1
                step.mark_failed(str(error))
            else:
                project = db.session.get(CodeProject, project_id)
                project.set_preview_images(images)
                project.status = CodeProjectStatus.PREVIEW_READY
                db.session.commit()
                recorder.emit(
                    AgentEventType.MODEL_RESPONSE,
                    step_id=step.id,
                    message=f"生成 {len(images)} 张预览图",
                    payload={"count": len(images)},
                )
                for image in images:
                    step.add_artifact(
                        AgentArtifactType.IMAGE,
                        image.get("id", "预览图"),
                        preview_url=image.get("url"),
                        mime_type="image/png",
                        domain_ref_type="code_project",
                        domain_ref_id=project_id,
                    )
                step.set_output(
                    output_summary=f"已生成 {len(images)} 张 UI 预览缩略图。",
                    next_action="在 Code 工作台选择并确认 UI 基调。",
                )
        # Continuity snapshot so the Context tab shows the ledger at every step.
        step.set_context(snapshot={"injected_text": "", "ledger": ledger.to_dict()})
    # Count the preview step as completed only when it succeeded or was
    # intentionally skipped; a failed preview is already counted in `failed`.
    if preview_ok:
        completed += 1
    progress("publish")

    # --- Step 7: Publisher ---------------------------------------------------
    with recorder.step(
        "publisher", "发布 Agent", "publisher", 7, input_summary="汇总并发布项目状态"
    ) as step:
        project = db.session.get(CodeProject, project_id)
        step.add_artifact(
            AgentArtifactType.JSON,
            "项目快照",
            content_json=project.to_dict(include_documents=True),
            filename="project.json",
        )
        # Durable record of the final consensus ledger (debug / audit).
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
            output_summary=f"项目「{project.title}」已发布，当前状态 {project.status}。",
            reasoning_summary="所有产物已写回 CodeProject / CodeDocument，上下文账本已固化，可在 Code 工作台继续编辑。",
            self_check=(
                f"需求/流程/文档({project.documents.count()})/风格 均已生成；"
                f"预览图 {'成功' if preview_ok else '失败或跳过'}。"
            ),
            next_action="在 Code 工作台确认 UI 基调。",
        )
    persist_ledger()
    completed += 1
    progress("done")

    status = AgentRunStatus.COMPLETED if preview_ok else AgentRunStatus.PARTIAL
    return {"status": status, "resource_id": project_id, "extra_credits": extra_credits}
