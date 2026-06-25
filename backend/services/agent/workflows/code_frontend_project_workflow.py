"""
Code domain workflow — ``code_frontend_project_generation``.

The frontend code-generation path (the removed single-file ``code_frontend_generation``
is gone): an autonomous coding CLI runs in a sandboxed container and produces a
COMPLETE multi-file React + Vite + TypeScript project that it builds and
self-checks. The CLI's stream-json is translated live into AgentEvents
(``file_created`` / ``tool_call`` / ``tool_result``), so the existing timeline UI
replays every file write and build command.

Two agents cooperate in the container: Claude Code writes the project, and when
the UI needs real raster imagery it triggers the bundled ``image-assets`` skill,
which shells out to the OpenAI Codex CLI to generate pictures via the image model
(see ``frontend_project_service``). Those ``gen-assets`` / Codex invocations are
surfaced on the timeline as distinct asset-generation events.

Steps: ``fe_planner`` -> ``fe_project_build`` -> ``fe_publish`` (3 counted steps;
the agent's own build + self-check loop replaces the removed single-file path's
critic/repair, and an advisory FR/NFR acceptance review runs at the end of the
build step — a PASS/CONCERNS/FAIL verdict with per-FR coverage that is recorded
and surfaced but never blocks publish). The build runs in
``frontend_project_service`` which owns the container lifecycle; this workflow
only assembles context, maps events, runs the review, and publishes the
deliverable (source zip + previewable dist).
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
from backend.models.code import CodeFigmaDesign, CodeProject, CodeProjectStatus
from backend.services import pricing
from backend.services.agent.context_ledger import ContextLedger, seed_from_inputs
from backend.services.agent.context_verifier import gate_available
from backend.services.agent.files import agent_run_dir
from backend.services.code.figma import storage as figma_storage
from backend.services.code.frontend_project_service import get_frontend_project_service
from backend.services.credit_service import charge

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


_CODE_EXTS = (".tsx", ".ts", ".jsx", ".js", ".css", ".html", ".json", ".md")
_REVIEW_MAX_FILE = 4000
_REVIEW_MAX_TOTAL = 60_000


def _source_digest(files: dict) -> str:
    """Concatenate the textual source files (skip binary assets) for acceptance review."""
    parts: list[str] = []
    total = 0
    for path in sorted(files):
        low = path.lower()
        if not low.endswith(_CODE_EXTS) or "node_modules/" in low or low.startswith("dist/"):
            continue
        raw = files[path]
        try:
            text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        except (UnicodeDecodeError, AttributeError):
            continue
        chunk = f"// ===== {path} =====\n{text[:_REVIEW_MAX_FILE]}"
        parts.append(chunk)
        total += len(chunk)
        if total >= _REVIEW_MAX_TOTAL:
            break
    return "\n\n".join(parts)[:_REVIEW_MAX_TOTAL]


def _load_figma_frames(project_id: str) -> list[dict]:
    """Frames of the project's attached Figma design for the build (name/ir/render).

    Returns ``[{name, ir_text, render_path}]`` (render_path = absolute path to the
    stored PNG), or ``[]`` when no design is attached.
    """
    design = CodeFigmaDesign.query.filter_by(project_id=project_id).first()
    if not design:
        return []
    frames: list[dict] = []
    for frame in design.get_frames():
        render_filename = frame.get("render_filename")
        frames.append(
            {
                "name": frame.get("name"),
                "ir_text": frame.get("ir_text") or "",
                "render_path": (
                    str(figma_storage.render_path(project_id, render_filename))
                    if render_filename
                    else None
                ),
            }
        )
    return frames


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
        elif not service.assets_enabled():
            recorder.emit(
                AgentEventType.PROGRESS,
                step_id=step.id,
                message="未配置 OPENAI_API_KEY：将跳过图片资源生成（改用 CSS/SVG，不使用 emoji）",
                payload={"assets_enabled": False},
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
        step.model_provider = "claude-code-cli + codex-cli"
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
                    elif name == "Skill":
                        skill = inp.get("command") or inp.get("name") or inp.get("skill") or ""
                        recorder.emit(
                            AgentEventType.TOOL_CALL, step_id=step.id,
                            message=f"触发技能:{skill or 'image-assets'}",
                            payload={"tool": name, "skill": skill},
                        )
                    else:
                        cmd = inp.get("command") or ""
                        # Claude shells out to Codex (via the image-assets skill)
                        # for real raster assets — surface that distinctly.
                        is_assets = "gen-assets" in cmd or "codex" in cmd
                        recorder.emit(
                            AgentEventType.TOOL_CALL, step_id=step.id,
                            message=(
                                "调用 Codex 生成资源图片(image-assets 技能)"
                                if is_assets else (f"{name}: {cmd[:80]}" if cmd else name)
                            ),
                            payload={
                                "tool": name, "command": cmd[:500],
                                **({"lane": "codex-assets"} if is_assets else {}),
                            },
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
            elif etype == "fe_phase":
                # Sentinel from the container's self-healing build ladder.
                phase = event.get("phase") or ""
                labels = {
                    "install": "安装依赖（npm install）",
                    "build": "运行构建（npm run build）",
                    "ai-repair": "构建未通过，启动 AI 定向修复",
                    "stub": "构建仍未通过，确定性补桩缺失模块",
                    "vite-only": "跳过类型检查，直接用 Vite 构建",
                    "fallback": "构建仍未通过，合成降级预览页以保证有产物",
                }
                is_recovery = phase in ("ai-repair", "stub", "vite-only", "fallback")
                recorder.emit(
                    AgentEventType.WARNING if is_recovery else AgentEventType.TOOL_CALL,
                    level=AgentEventLevel.WARNING if is_recovery else AgentEventLevel.INFO,
                    step_id=step.id,
                    message=labels.get(phase, f"构建阶段：{phase}"),
                    payload={"phase": phase},
                )

        # If a Figma design is attached to this project, feed it (render images +
        # IR) into the build so the generated React project matches the design.
        figma_frames = _load_figma_frames(project_id)
        if figma_frames:
            recorder.emit(
                AgentEventType.PROGRESS,
                step_id=step.id,
                message=f"按关联的 Figma 设计生成({len(figma_frames)} 个画板)",
                payload={"figma_frames": len(figma_frames)},
            )

        # Full-stack mode: if a shared API contract was synthesized for this
        # project, inject it so the generated frontend calls the REAL backend
        # (via window.__API_BASE__) instead of localStorage. Empty otherwise.
        from backend.services.code.fullstack import contract_service

        _ledger_row = contract_service.get_ledger(project_id)
        contract_block = ""
        if _ledger_row and _ledger_row.contract_status == "ready":
            contract_block = contract_service.render_contract_for_prompt(
                _ledger_row.get_api_contract(), include_db_schema=False
            )
            recorder.emit(
                AgentEventType.PROGRESS, step_id=step.id,
                message="全栈模式:已注入共享 API 契约,前端将调用真实后端(window.__API_BASE__)",
                payload={"fullstack": True},
            )

        result = service.build_project(
            requirement=project.requirement_input,
            requirements_doc=project.requirements_doc,
            development_flow=project.development_flow or "",
            documents_digest=_documents_digest(project),
            style_prompt=project.style_prompt or "",
            ui_baseline_prompt=project.ui_baseline_prompt or "",
            context_ledger=injected,
            contract_block=contract_block,
            figma_frames=figma_frames,
            on_event=on_event,
            is_cancelled=ctx.is_cancelled,
        )

        if result.get("error") == "cancelled":
            return cancel_result(project_id)
        # The container always synthesizes a previewable dist, so a hard failure
        # here means the agent produced no source at all (config/auth) — runtime
        # refunds it. A degraded-but-published run is NOT a failure.
        if not result.get("success"):
            raise RuntimeError(f"前端工程生成失败：{result.get('error') or '未知错误'}")

        degraded_reason = result.get("degraded_reason")
        _DEGRADED_LABELS = {
            "ai-repair": "首轮构建未通过，经 AI 定向修复后构建成功",
            "stub": "构建经确定性补桩（缺失模块占位）后成功，部分模块为占位实现",
            "vite-only": "跳过类型检查后由 Vite 构建成功，可能存在未解决的类型问题",
            "fallback": "多轮修复后构建仍未通过，已合成降级预览页（源码可下载）",
        }
        if result.get("degraded"):
            recorder.emit(
                AgentEventType.WARNING,
                level=AgentEventLevel.WARNING,
                step_id=step.id,
                message=f"前端工程以降级方式产出：{_DEGRADED_LABELS.get(degraded_reason, degraded_reason)}",
                payload={"degraded_reason": degraded_reason},
            )

        usage = result.get("usage") or {}
        src_files = result.get("files") or {}
        n_src = len(src_files)
        n_dist = len(result.get("dist_files") or {})
        # Raster assets generated by the image-assets skill (Codex -> image model).
        _ASSET_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".ico", ".bmp")
        n_assets = sum(1 for key in src_files if key.lower().endswith(_ASSET_EXTS))

        # Asset-lane diagnostics: explain a "no images" run instead of failing
        # silently — distinguish image-not-rebuilt / no-key / agent-didn't-call.
        lane = result.get("asset_lane") or {}
        if not lane.get("codex_available", True):
            recorder.emit(
                AgentEventType.WARNING, level=AgentEventLevel.WARNING, step_id=step.id,
                message="fe-agent 镜像内未找到 Codex CLI：未生成图片资源。"
                "请重建镜像后重试：docker compose --profile setup build（或 "
                "docker build -t fe-agent:latest backend/docker/fe-agent）。",
                payload={"asset_lane": lane},
            )
        elif not lane.get("openai_key", True):
            recorder.emit(
                AgentEventType.WARNING, level=AgentEventLevel.WARNING, step_id=step.id,
                message="未配置 OPENAI_API_KEY：未生成图片资源（已用 CSS/SVG 兜底）。",
                payload={"asset_lane": lane},
            )
        elif lane.get("calls", 0) == 0:
            recorder.emit(
                AgentEventType.WARNING, level=AgentEventLevel.WARNING, step_id=step.id,
                message="本次 Claude 未触发图片资源生成（gen-assets 调用 0 次）：未产出图片资源。",
                payload={"asset_lane": lane},
            )
        elif n_assets:
            recorder.emit(
                AgentEventType.PROGRESS, step_id=step.id,
                message=f"图片资源生成：gen-assets 调用 {lane.get('calls')} 次，产出 {n_assets} 张图片资源。",
                payload={"asset_lane": lane},
            )

        # --- Acceptance review: FR/NFR coverage vs the generated source --------
        # Advisory gate (never blocks publish): one text-model call rates the
        # project against the ledger's FR/NFR registry + style and emits a
        # PASS/CONCERNS/FAIL verdict with per-FR coverage. Charged per call;
        # skipped when no text provider is configured or no requirements are
        # registered (e.g. a run not preceded by a full-generation pass).
        reqs = ledger.to_dict().get("requirements") or []
        if reqs and src_files and gate_available() and charge(
            user_id=ctx.user_id,
            amount=pricing.CODE_CONTEXT_VERIFY,
            operation="code_context_verify",
            resource_type="agent_run",
            resource_id=ctx.run_id,
            description="project acceptance review",
            team_id=ctx.team_id,
        ):
            registry = "\n".join(f"- [{r['id']}] {r['statement']}" for r in reqs)
            review = service.review_project(
                source_digest=_source_digest(src_files),
                requirements_registry=registry,
                style_prompt=project.style_prompt or "",
            )
            if review:
                verdict = str(review.get("verdict") or "").upper()
                cov = review.get("fr_coverage") or []
                missing = [
                    c.get("id") for c in cov
                    if isinstance(c, dict) and not c.get("covered") and c.get("id")
                ]
                concern = verdict in ("CONCERNS", "FAIL")
                recorder.emit(
                    AgentEventType.WARNING if concern else AgentEventType.PROGRESS,
                    level=AgentEventLevel.WARNING if concern else AgentEventLevel.INFO,
                    step_id=step.id,
                    message=(
                        f"验收评审：{verdict or '—'}"
                        + (f"；未覆盖 {', '.join(missing)}" if missing else "；FR 覆盖完整")
                    ),
                    payload={
                        "verdict": verdict,
                        "missing_fr": missing,
                        "issues": (review.get("issues") or [])[:20],
                        "summary": review.get("summary"),
                    },
                )
                step.add_artifact(
                    AgentArtifactType.JSON, "前端工程验收评审",
                    content_json=review, filename="frontend_project_review.json",
                    # Distinct ref type: the acceptance review must NOT collide with
                    # the publish step's project-meta artifact (also JSON), or the
                    # frontend preview picker (.find by domain_ref_type) grabs this
                    # review (no preview_url) and renders no preview.
                    domain_ref_type="code_frontend_project_review", domain_ref_id=project_id,
                )

        step.model_response = (result.get("summary") or "")[:8000]
        db.session.commit()
        recorder.emit(
            AgentEventType.MODEL_RESPONSE, step_id=step.id, message="agent 完成",
            payload={"summary": result.get("summary"), "usage": usage,
                     "cost_usd": result.get("cost_usd"),
                     "degraded_reason": degraded_reason},
        )
        _degraded_note = f"（降级：{_DEGRADED_LABELS.get(degraded_reason, degraded_reason)}）" if result.get("degraded") else ""
        _asset_note = f"，其中 {n_assets} 张为 Codex 生成的图片资源" if n_assets else ""
        step.set_output(
            output_summary=f"已生成完整前端工程：{n_src} 个源码文件{_asset_note}，{n_dist} 个构建产物{_degraded_note}。{result.get('summary', '')}".strip(),
            reasoning_summary="沙箱容器内 Claude Code 自主创建多文件 React/Vite/TS 工程（需要真实图片时经 image-assets 技能触发 Codex 生成位图资源），并经自愈构建梯队（AI 修复 → 确定性补桩 → Vite 兜底 → 合成降级页）确保产出可预览 dist。",
            self_check=f"源码 {n_src} 文件（图片资源 {n_assets} 张）；dist {n_dist} 文件；cost≈${result.get('cost_usd')}；降级={degraded_reason or '无'}",
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

        # Source -> downloadable zip artifact. Values are bytes (binary-safe), so
        # generated raster assets survive into the zip / GitHub commit intact.
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for rel, content in files.items():
                archive.writestr(rel, content or b"")
        step.add_artifact(
            AgentArtifactType.TEXT, "前端工程源码（zip）",
            filename="frontend_project.zip", mime_type="application/zip",
            write_file=True, content_bytes=buffer.getvalue(),
            domain_ref_type="code_frontend_project_zip", domain_ref_id=project_id,
        )

        # Built dist -> on-disk site dir for iframe preview (preserve assets/ tree).
        # Bytes (binary-safe) so bundled images render in the preview iframe.
        site_dir = agent_run_dir(ctx.run_id) / "site"
        site_dir.mkdir(parents=True, exist_ok=True)
        for rel, content in dist_files.items():
            target = site_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content or b"")
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
                "degraded": result.get("degraded", False),
                "degraded_reason": result.get("degraded_reason"),
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
