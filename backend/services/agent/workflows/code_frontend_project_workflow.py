"""
Code domain workflow — ``code_frontend_project_generation``.

The agentic alternative to ``code_frontend_generation`` (which produces a single
inline index.html): an autonomous coding CLI runs in a sandboxed container and
produces a COMPLETE multi-file React + Vite + TypeScript project that it builds
and self-checks. The CLI's stream-json is translated live into AgentEvents
(``file_created`` / ``tool_call`` / ``tool_result``), so the existing timeline UI
replays every file write and build command.

Steps: ``fe_planner`` -> ``fe_project_build`` -> ``fe_publish`` (3 counted steps;
the agent's own build + self-check loop subsumes the separate critic/repair of
the single-file path). The build runs in ``frontend_project_service`` which owns
the container lifecycle; this workflow only assembles context, maps events, and
publishes the deliverable (source zip + previewable dist).
"""
import io
import json
import logging
import zipfile

from backend.extensions import db
from backend.models.agent import (
    AgentArtifactType,
    AgentEventLevel,
    AgentEventType,
    AgentRun,
    AgentRunStatus,
)
from backend.models.code import CodeProject, CodeProjectStatus
from backend.services.agent.context_ledger import ContextLedger, seed_from_inputs
from backend.services.agent.files import agent_run_dir
from backend.services.code.frontend_project_service import get_frontend_project_service

logger = logging.getLogger(__name__)

TOTAL_STEPS = 3  # planner, build, publish
_MAX_DOC_CHARS = 1500
_MAX_DIGEST_CHARS = 12_000


def _documents_digest(project: CodeProject) -> str:
    parts = []
    for document in project.documents.all():
        body = (document.content or "")[:_MAX_DOC_CHARS]
        parts.append(f"## {document.title} ({document.document_type})\n{body}")
    return "\n\n".join(parts)[:_MAX_DIGEST_CHARS]


def run_code_frontend_project_workflow(ctx, recorder) -> dict:
    """Generate + build + publish a runnable multi-file frontend project."""
    service = get_frontend_project_service()
    completed = 0
    ledger = ContextLedger.empty()

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

    # --- Step 1: Planner -----------------------------------------------------
    with recorder.step(
        "fe_planner", "前端规划 Agent", "planner", 1, input_summary="校验已确认项目并准备构建上下文"
    ) as step:
        if not ctx.resource_id:
            raise ValueError("缺少 resource_id：前端项目生成需要一个已有的 Code 项目")
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

        # Reuse the consensus ledger from the most recent full-generation run.
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
        fe_run = db.session.get(AgentRun, ctx.run_id)
        fe_run.set_context_ledger(ledger.to_dict())
        db.session.commit()

        if not service.is_configured():
            recorder.emit(
                AgentEventType.WARNING,
                level=AgentEventLevel.WARNING,
                step_id=step.id,
                message="未配置 ANTHROPIC_API_KEY，无法运行容器化前端生成",
            )

        step.set_context(snapshot={"injected_text": "", "ledger": ledger.to_dict()})
        step.set_output(
            output_summary=f"已校验项目「{project.title}」，准备生成完整前端工程。",
            reasoning_summary="确认需求/风格齐备并汇总文档；载入上一轮共识账本，保证口径一致。",
            decision_notes="技术栈固定为 React + Vite + TypeScript 多文件工程，由沙箱内 agent 自行构建与自检。",
            self_check=f"文档 {project.documents.count()} 份；UI 基调确认：{project.status == CodeProjectStatus.UI_CONFIRMED}。",
            next_action="在沙箱容器内生成完整前端项目。",
        )
    completed += 1
    progress("build")

    # --- Step 2: Container build (agentic) -----------------------------------
    if ctx.is_cancelled():
        return cancel_result(project_id)
    result: dict = {}
    with recorder.step(
        "fe_project_build", "前端工程 Agent", "generator", 2,
        input_summary="在沙箱容器内自主生成 + 构建完整前端工程",
    ) as step:
        project = db.session.get(CodeProject, project_id)
        injected = ledger.render_for_prompt()
        step.model_provider = "claude-code-cli"
        db.session.commit()

        def on_event(event: dict) -> None:
            """Translate the CLI's stream-json into AgentEvents (live timeline)."""
            etype = event.get("type")
            if etype == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    name = block.get("name") or ""
                    inp = block.get("input") or {}
                    if name in ("Write", "Edit"):
                        fpath = inp.get("file_path") or inp.get("path") or ""
                        recorder.emit(
                            AgentEventType.FILE_CREATED, step_id=step.id,
                            message=f"写入 {fpath}",
                            payload={"tool": name, "file": fpath},
                        )
                    else:
                        cmd = inp.get("command") or ""
                        recorder.emit(
                            AgentEventType.TOOL_CALL, step_id=step.id,
                            message=(f"{name}: {cmd[:80]}" if cmd else name),
                            payload={"tool": name, "command": cmd[:500]},
                        )
            elif etype == "user":
                for block in event.get("message", {}).get("content", []):
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    content = block.get("content")
                    text = content if isinstance(content, str) else json.dumps(
                        content, ensure_ascii=False
                    )
                    recorder.emit(
                        AgentEventType.TOOL_RESULT, step_id=step.id,
                        message="工具返回", payload={"output": (text or "")[:2000]},
                    )

        result = service.build_project(
            requirement=project.requirement_input,
            requirements_doc=project.requirements_doc,
            development_flow=project.development_flow or "",
            documents_digest=_documents_digest(project),
            style_prompt=project.style_prompt or "",
            ui_baseline_prompt=project.ui_baseline_prompt or "",
            context_ledger=injected,
            on_event=on_event,
            is_cancelled=ctx.is_cancelled,
        )

        if result.get("error") == "cancelled":
            return cancel_result(project_id)
        if not result.get("success"):
            raise RuntimeError(f"前端工程生成失败：{result.get('error') or '未知错误'}")

        usage = result.get("usage") or {}
        n_src = len(result.get("files") or {})
        n_dist = len(result.get("dist_files") or {})
        step.model_response = (result.get("summary") or "")[:8000]
        db.session.commit()
        recorder.emit(
            AgentEventType.MODEL_RESPONSE, step_id=step.id, message="agent 完成",
            payload={"summary": result.get("summary"), "usage": usage,
                     "cost_usd": result.get("cost_usd")},
        )
        step.set_output(
            output_summary=f"已生成完整前端工程：{n_src} 个源码文件，{n_dist} 个构建产物。{result.get('summary', '')}".strip(),
            reasoning_summary="沙箱容器内的编码 agent 自主创建多文件 React/Vite/TS 工程，并自行 npm install + build 自检直至通过。",
            self_check=f"源码 {n_src} 文件；dist {n_dist} 文件；cost≈${result.get('cost_usd')}",
            next_action="发布并提供预览。",
        )
    completed += 1
    progress("publish")

    # --- Step 3: Publish (source zip + previewable dist) ---------------------
    with recorder.step(
        "fe_publish", "发布 Agent", "publisher", 3, input_summary="保存源码 zip 与可预览 dist"
    ) as step:
        files = result.get("files") or {}
        dist_files = result.get("dist_files") or {}

        # Source -> downloadable zip artifact.
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for rel, content in files.items():
                archive.writestr(rel, content or "")
        step.add_artifact(
            AgentArtifactType.TEXT, "前端工程源码（zip）",
            filename="frontend_project.zip", mime_type="application/zip",
            write_file=True, content_bytes=buffer.getvalue(),
            domain_ref_type="code_frontend_project_zip", domain_ref_id=project_id,
        )

        # Built dist -> on-disk site dir for iframe preview (preserve assets/ tree).
        site_dir = agent_run_dir(ctx.run_id) / "site"
        site_dir.mkdir(parents=True, exist_ok=True)
        for rel, content in dist_files.items():
            target = site_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content or "", encoding="utf-8")
        preview_url = f"/api/agent/runs/{ctx.run_id}/site/index.html"

        step.add_artifact(
            AgentArtifactType.JSON, "前端工程元数据",
            content_json={
                "source_files": sorted(files.keys()),
                "dist_files": sorted(dist_files.keys()),
                "preview_url": preview_url,
                "cost_usd": result.get("cost_usd"),
                "usage": result.get("usage"),
                "summary": result.get("summary"),
                "delivery": "multi-file-project",
            },
            filename="frontend_project_meta.json",
            preview_url=preview_url,
            domain_ref_type="code_frontend_project_meta", domain_ref_id=project_id,
        )
        step.set_context(snapshot={"injected_text": "", "ledger": ledger.to_dict()})
        step.set_output(
            output_summary="前端工程已发布：可在右侧预览构建结果或下载源码 zip。",
            reasoning_summary="把源码打包为 zip artifact，构建产物 dist 写入站点目录供 iframe 预览。",
            self_check=f"源码 {len(files)} 文件；dist {len(dist_files)} 文件；preview={preview_url}",
            next_action="在预览面板交互验证，或下载源码。",
        )
    completed += 1
    progress("done")

    return {"status": AgentRunStatus.COMPLETED, "resource_id": project_id}
