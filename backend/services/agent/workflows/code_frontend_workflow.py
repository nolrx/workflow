"""
Code domain workflow — ``code_frontend_generation``.

The phase AFTER UI-baseline confirmation: generate a complete, fully-interactive
single-file HTML app from the confirmed spec, then adversarially review it (and
repair once if it fails the interactivity/completeness bar). The final
`index.html` is stored inline in the artifact row and written to local artifact
storage (`domain_ref_type = "code_frontend_html"`).
"""
import logging

from backend.extensions import db
from backend.models.agent import (
    AgentArtifactType,
    AgentEventLevel,
    AgentEventType,
    AgentRun,
    AgentRunStatus,
)
from backend.models.code import CodeProject, CodeProjectStatus
from backend.services import pricing
from backend.services.agent.context_ledger import ContextLedger, seed_from_inputs
from backend.services.agent.context_verifier import (
    emit_context_events,
    gate_available,
    run_ai_consistency_gate,
    run_deterministic_checks,
)
from backend.services.code.frontend_build_service import get_frontend_build_service
from backend.services.credit_service import charge

logger = logging.getLogger(__name__)

TOTAL_STEPS = 4  # planner, build, critic, publish (repair is an uncounted extra)
_MAX_DOC_CHARS = 1500
_MAX_DIGEST_CHARS = 12_000


def _documents_digest(project: CodeProject) -> str:
    parts = []
    for document in project.documents.all():
        body = (document.content or "")[:_MAX_DOC_CHARS]
        parts.append(f"## {document.title} ({document.document_type})\n{body}")
    digest = "\n\n".join(parts)
    return digest[:_MAX_DIGEST_CHARS]


def run_code_frontend_workflow(ctx, recorder) -> dict:
    """Generate + review + publish a runnable frontend project for a Code project."""
    service = get_frontend_build_service()
    completed = 0
    extra_credits = 0  # per-call context-verify gate charges, surfaced to runtime
    ledger = ContextLedger.empty()  # reloaded from the prior full-generation run

    def progress(current_step: str) -> None:
        run = db.session.get(AgentRun, ctx.run_id)
        if run:
            run.set_progress(
                {
                    "total_steps": TOTAL_STEPS,
                    "completed_steps": completed,
                    "failed_steps": 0,
                    "current_step": current_step,
                }
            )
            db.session.commit()
        recorder.emit(
            AgentEventType.PROGRESS,
            message=f"进度 {completed}/{TOTAL_STEPS}",
            payload={"completed": completed, "total": TOTAL_STEPS, "current": current_step},
        )

    def cancel_result(project_id) -> dict:
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

    # --- Step 1: Planner (validate confirmed project, assemble build context) ---
    with recorder.step(
        "fe_planner", "前端规划 Agent", "planner", 1, input_summary="校验已确认项目并准备构建上下文"
    ) as step:
        if not ctx.resource_id:
            raise ValueError("缺少 resource_id：前端生成需要一个已有的 Code 项目")
        project = CodeProject.query.filter_by(id=ctx.resource_id, user_id=ctx.user_id).first()
        if not project:
            raise ValueError("项目不存在或无权访问")
        if not project.requirements_doc or not project.style_prompt:
            raise ValueError("项目尚未完成需求文档与风格文档，无法生成前端")
        if project.status != CodeProjectStatus.UI_CONFIRMED:
            recorder.emit(
                AgentEventType.WARNING,
                level=AgentEventLevel.WARNING,
                step_id=step.id,
                message="项目尚未确认 UI 基调（ui_confirmed），将按当前内容生成前端",
            )

        project_id = project.id

        # Reload the consensus ledger from the most recent full-generation run for
        # this project (separate run → no shared in-memory ledger). Seed from the
        # project if there is none, so the frontend run always has a baseline.
        prior = (
            AgentRun.query.filter_by(
                resource_id=project_id, workflow="code_full_generation"
            )
            .order_by(AgentRun.created_at.desc())
            .first()
        )
        ledger = ContextLedger.load(prior.get_context_ledger() if prior else None)
        if ledger.is_empty():
            ledger = seed_from_inputs(
                project.requirement_input, project.title, project.get_selected_style_ids()
            )
        fe_run = db.session.get(AgentRun, ctx.run_id)
        fe_run.set_context_ledger(ledger.to_dict())
        db.session.commit()

        plan = {
            "project_id": project_id,
            "stack": "Single-file HTML + inline CSS + browser-native JavaScript",
            "inputs": {
                "has_requirements": bool(project.requirements_doc),
                "has_flow": bool(project.development_flow),
                "documents": project.documents.count(),
                "has_style": bool(project.style_prompt),
                "ui_confirmed": project.status == CodeProjectStatus.UI_CONFIRMED,
            },
            "hard_constraints": [
                "所有组件可交互（按钮/输入/表单均已接线）",
                "功能完整（无 TODO/占位/未实现）",
                "保存为 index.html 后可直接在浏览器运行",
            ],
        }
        step.add_artifact(
            AgentArtifactType.JSON, "前端构建计划", content_json=plan, filename="frontend_plan.json"
        )
        step.set_context(snapshot={"injected_text": "", "ledger": ledger.to_dict()})
        step.set_output(
            output_summary=f"已校验项目「{project.title}」，准备生成前端。",
            reasoning_summary="先确认需求/风格齐备并汇总文档作为构建上下文，并载入上一轮的共识账本，确保生成有据可依、口径一致。",
            decision_notes="技术栈固定为单文件 HTML，CSS/JavaScript 均内联，便于直接保存、预览和下载。",
            self_check=f"文档 {project.documents.count()} 份；UI 基调确认：{project.status == CodeProjectStatus.UI_CONFIRMED}。",
            next_action="生成完整前端项目。",
        )
    completed += 1
    progress("build")

    # --- Step 2: Build the project ------------------------------------------
    if ctx.is_cancelled():
        return cancel_result(project_id)
    with recorder.step(
        "fe_build", "前端生成 Agent", "generator", 2, input_summary="基于确认内容生成完整可交互前端"
    ) as step:
        project = db.session.get(CodeProject, project_id)
        injected = ledger.render_for_prompt()
        result = service.build_app(
            requirement=project.requirement_input,
            requirements_doc=project.requirements_doc,
            development_flow=project.development_flow or "",
            documents_digest=_documents_digest(project),
            style_prompt=project.style_prompt or "",
            ui_baseline_prompt=project.ui_baseline_prompt or "",
            context_ledger=injected,
            on_model_call=step.model_tracer(),
        )
        html = result["html"]

        # Consistency at the style→frontend-code boundary. Deterministic stack
        # conformance always; AI gate only on a real (non-fallback) build, charged
        # per call. The gate deliberately does NOT use step.model_tracer() so the
        # build prompt/response trace is preserved.
        build_summary = (
            f"{result.get('summary', '')}\n产物: index.html\n"
            f"HTML 字符数: {len(html)}"
        ).strip()
        det = run_deterministic_checks(
            step_key="frontend_build",
            ledger=ledger,
            new_output={"frontend_summary": build_summary, "files": ["index.html"]},
            expectations={
                "stack_conformance": {"must_include": ["html"], "must_have_file": "index.html"},
            },
        )
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
            description="context verify @ frontend_build",
            team_id=ctx.team_id,
        ):
            extra_credits += pricing.CODE_CONTEXT_VERIFY
            ai_result = run_ai_consistency_gate(
                ledger=ledger, new_product_summary=build_summary, step_key="frontend_build"
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
            output_summary=f"已生成 index.html。{result.get('summary', '')}".strip(),
            reasoning_summary="按需求/文档/风格与共识账本生成单文件 HTML 应用，所有交互用浏览器原生 JavaScript 接线，并对照账本做一致性校验。",
            self_check=f"产物 index.html；HTML 字符数：{len(html)}；{det['summary']}",
            next_action="审查可交互性与完整性。",
        )
    completed += 1
    progress("critic")

    # --- Step 3: Critic ------------------------------------------------------
    if ctx.is_cancelled():
        return cancel_result(project_id)
    verdict = {"passed": True, "issues": [], "summary": ""}
    with recorder.step(
        "fe_critic", "审查 Agent", "critic", 3, input_summary="复检所有组件是否可交互、功能是否完整"
    ) as step:
        verdict = service.critique_app(html, on_model_call=step.model_tracer())
        step.add_artifact(
            AgentArtifactType.JSON,
            "审查结果",
            content_json=verdict,
            filename="frontend_review.json",
        )
        issues = verdict.get("issues") or []
        step.set_output(
            output_summary=("通过：所有控件可交互、功能完整。" if verdict.get("passed") else f"未通过，发现 {len(issues)} 处问题。"),
            reasoning_summary="逐项检查 HTML 中的死按钮、未接线输入、占位/未实现、未使用状态与无法运行项。",
            self_check=verdict.get("summary") or "",
            next_action=("发布。" if verdict.get("passed") else "修复后发布。"),
        )
    completed += 1

    # --- Step 3b: Repair (only if the critic rejected) ----------------------
    if not verdict.get("passed") and verdict.get("issues"):
        if ctx.is_cancelled():
            return cancel_result(project_id)
        progress("repair")
        with recorder.step(
            "fe_repair", "修复 Agent", "generator", 4, input_summary="按审查意见修复 HTML 并补全功能"
        ) as step:
            repaired = service.repair_app(
                html, verdict["issues"], on_model_call=step.model_tracer()
            )
            html = repaired["html"]
            step.set_output(
                output_summary=f"已按 {len(verdict['issues'])} 处意见修复。{repaired.get('summary', '')}".strip(),
                reasoning_summary="针对审查问题重写 HTML/CSS/JavaScript，确保交互与功能完整。",
                next_action="发布。",
            )

    # --- Step 4: Publish (persist the final index.html) ----------------------
    progress("publish")
    with recorder.step(
        "fe_publish", "发布 Agent", "publisher", 5, input_summary="保存并发布可预览的 HTML 文件"
    ) as step:
        step.add_artifact(
            AgentArtifactType.TEXT,
            "前端 HTML（可预览）",
            content_text=html,
            filename="index.html",
            mime_type="text/html; charset=utf-8",
            write_file=True,
            domain_ref_type="code_frontend_html",
            domain_ref_id=project_id,
        )
        step.add_artifact(
            AgentArtifactType.JSON,
            "前端 HTML 元数据",
            content_json={
                "filename": "index.html",
                "review_passed": bool(verdict.get("passed")),
                "html_chars": len(html),
                "delivery": "single-html",
            },
            filename="frontend_html_meta.json",
            domain_ref_type="code_frontend_meta",
            domain_ref_id=project_id,
        )
        step.set_context(snapshot={"injected_text": "", "ledger": ledger.to_dict()})
        step.set_output(
            output_summary="index.html 已发布，可在右侧预览或下载。",
            reasoning_summary="把最终 HTML 存入数据库 artifact，并写入本地文件存储，供浏览器内 iframe 预览和文件下载。",
            self_check=f"审查通过：{bool(verdict.get('passed'))}；HTML 字符数：{len(html)}",
            next_action="在预览面板交互验证，或下载 HTML 文件。",
        )
    completed += 1
    progress("done")

    return {
        "status": AgentRunStatus.COMPLETED,
        "resource_id": project_id,
        "extra_credits": extra_credits,
    }
