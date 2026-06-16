"""
Code domain workflow — ``code_frontend_generation``.

The phase AFTER UI-baseline confirmation: generate a complete, fully-interactive
React + TypeScript frontend project from the confirmed spec, then adversarially
review it (and repair once if it fails the interactivity/completeness bar). The
final Sandpack-ready file map is emitted as a single JSON artifact
(``domain_ref_type = "code_frontend"``) that the standalone preview component
feeds into Sandpack.
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
from backend.services.code.frontend_build_service import get_frontend_build_service

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
        return {"status": AgentRunStatus.CANCELLED, "resource_id": project_id}

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
        digest = _documents_digest(project)
        plan = {
            "project_id": project_id,
            "stack": "React 18 + TypeScript + plain CSS (Sandpack react-ts)",
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
                "仅依赖 react，可在 Sandpack 直接运行",
            ],
        }
        step.add_artifact(
            AgentArtifactType.JSON, "前端构建计划", content_json=plan, filename="frontend_plan.json"
        )
        step.set_output(
            output_summary=f"已校验项目「{project.title}」，准备生成前端。",
            reasoning_summary="先确认需求/风格齐备并汇总文档作为构建上下文，确保生成有据可依。",
            decision_notes="技术栈固定为 React+TS+plain CSS，仅依赖 react，以保证 Sandpack 内可直接运行。",
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
        result = service.build_app(
            requirement=project.requirement_input,
            requirements_doc=project.requirements_doc,
            development_flow=project.development_flow or "",
            documents_digest=_documents_digest(project),
            style_prompt=project.style_prompt or "",
            ui_baseline_prompt=project.ui_baseline_prompt or "",
            on_model_call=step.model_tracer(),
        )
        files = result["files"]
        for path, content in files.items():
            step.add_artifact(
                AgentArtifactType.TEXT,
                path,
                content_text=content,
                filename=path.lstrip("/").replace("/", "__"),
                mime_type="text/plain",
                write_file=True,
                domain_ref_type="code_frontend_file",
                domain_ref_id=project_id,
            )
        if result.get("used_fallback"):
            recorder.emit(
                AgentEventType.WARNING,
                level=AgentEventLevel.WARNING,
                step_id=step.id,
                message="AI 不可用，使用内置可交互示例作为前端占位",
            )
        step.set_output(
            output_summary=f"已生成 {len(files)} 个文件。{result.get('summary', '')}".strip(),
            reasoning_summary="按需求/文档/风格生成 React+TS 单页应用，所有交互用状态接线。",
            self_check=f"入口 {result.get('entry')}；文件：{', '.join(files.keys())}",
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
        verdict = service.critique_app(files, on_model_call=step.model_tracer())
        step.add_artifact(
            AgentArtifactType.JSON,
            "审查结果",
            content_json=verdict,
            filename="frontend_review.json",
        )
        issues = verdict.get("issues") or []
        step.set_output(
            output_summary=("通过：所有组件可交互、功能完整。" if verdict.get("passed") else f"未通过，发现 {len(issues)} 处问题。"),
            reasoning_summary="逐文件检查死按钮、未接线输入、占位/未实现、未使用组件与无法运行项。",
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
            "fe_repair", "修复 Agent", "generator", 4, input_summary="按审查意见修复并补全功能"
        ) as step:
            repaired = service.repair_app(
                files, verdict["issues"], on_model_call=step.model_tracer()
            )
            files = repaired["files"]
            for path, content in files.items():
                step.add_artifact(
                    AgentArtifactType.TEXT,
                    path,
                    content_text=content,
                    filename=path.lstrip("/").replace("/", "__"),
                    mime_type="text/plain",
                    write_file=True,
                    domain_ref_type="code_frontend_file",
                    domain_ref_id=project_id,
                )
            step.set_output(
                output_summary=f"已按 {len(verdict['issues'])} 处意见修复。{repaired.get('summary', '')}".strip(),
                reasoning_summary="针对审查问题重写相关文件，确保交互与功能完整。",
                next_action="发布。",
            )

    # --- Step 4: Publish (emit the Sandpack-ready file map) ------------------
    progress("publish")
    with recorder.step(
        "fe_publish", "发布 Agent", "publisher", 5, input_summary="汇总并发布可预览的前端工程"
    ) as step:
        bundle = {
            "files": files,
            "entry": "/App.tsx",
            "components": sorted(files.keys()),
            "review_passed": bool(verdict.get("passed")),
            "template": "react-ts",
        }
        step.add_artifact(
            AgentArtifactType.JSON,
            "前端工程（可预览）",
            content_json=bundle,
            filename="frontend_project.json",
            domain_ref_type="code_frontend",
            domain_ref_id=project_id,
        )
        step.set_output(
            output_summary=f"前端工程已发布，共 {len(files)} 个文件，可在右侧 Sandpack 预览。",
            reasoning_summary="把最终文件映射打包为 Sandpack react-ts 工程，供浏览器内实时预览。",
            self_check=f"审查通过：{bool(verdict.get('passed'))}；文件：{', '.join(files.keys())}",
            next_action="在预览面板交互验证，或下载工程。",
        )
    completed += 1
    progress("done")

    return {"status": AgentRunStatus.COMPLETED, "resource_id": project_id}
