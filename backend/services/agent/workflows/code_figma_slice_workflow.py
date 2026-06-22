"""
Code domain workflow — ``code_figma_slice_generation``.

Turns a Code project's preview thumbnail into an EDITABLE Figma design instead
of a single flat image. An OpenAI Codex CLI runs headless in a sandboxed
container (see ``figma_slice_service``), analyses the thumbnail into a Design IR
layer tree, and a deterministic Pillow step crops the IMAGE regions out of the
source PNG. The result is published as a plugin-consumable payload artifact
(``code_figma_slice_payload``) that the ``/export?source=sliced`` endpoint wraps
into a one-time pairing code for the companion Figma plugin.

Steps: ``fe_slice_planner`` -> ``fe_slice_analyze`` -> ``fe_slice_publish``
(3 counted steps). The container always yields a previewable IR — on any agent
failure it falls back to a single-image IR (== the legacy preview_image export),
so a published-but-degraded run is NOT a failure.
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
from backend.models.code import CodeProject
from backend.services.agent.context_ledger import ContextLedger, seed_from_inputs
from backend.services.code.figma_export_service import ExportError, select_preview_data_url
from backend.services.code.figma_slice_service import (
    build_slice_payload,
    get_figma_slice_service,
)

logger = logging.getLogger(__name__)

TOTAL_STEPS = 3  # planner, analyze, publish


def run_code_figma_slice_workflow(ctx, recorder) -> dict:
    """Slice + analyze a preview thumbnail into an editable Figma payload."""
    service = get_figma_slice_service()
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
        "fe_slice_planner", "切片规划 Agent", "planner", 1,
        input_summary="校验项目并选定要切片的预览图",
    ) as step:
        if not ctx.resource_id:
            raise ValueError("缺少 resource_id：切片导出需要一个已有的 Code 项目")
        project = CodeProject.query.filter_by(id=ctx.resource_id, user_id=ctx.user_id).first()
        if not project:
            raise ValueError("项目不存在或无权访问")
        project_id = project.id

        try:
            image_data_url = select_preview_data_url(project, (ctx.config or {}).get("preview_id"))
        except ExportError as exc:
            raise ValueError(exc.message) from exc

        # Reuse the consensus ledger from the most recent full-generation run so
        # naming / palette佐证 stays consistent with the rest of the project.
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

        if not service.is_configured():
            recorder.emit(
                AgentEventType.WARNING,
                level=AgentEventLevel.WARNING,
                step_id=step.id,
                message="未配置 OPENAI_API_KEY，将无法运行容器化切片分析（导出会降级为整图）",
            )

        step.set_context(snapshot={"injected_text": "", "ledger": ledger.to_dict()})
        step.set_output(
            output_summary=f"已选定项目「{project.title}」的预览图，准备切片分析。",
            reasoning_summary="校验项目并按 preview_id→已确认→第一张的优先级选图；载入上一轮共识账本。",
            decision_notes="由沙箱容器内的 Codex CLI 对缩略图做切片与内容分析，产出可编辑 Design IR。",
            self_check=f"预览图已选定；OPENAI_API_KEY 配置：{service.is_configured()}。",
            next_action="在沙箱容器内运行切片分析。",
        )
    completed += 1
    progress("analyze")

    # --- Step 2: Container slice + analyze (agentic) -------------------------
    if ctx.is_cancelled():
        return cancel_result(project_id)
    result: dict = {}
    with recorder.step(
        "fe_slice_analyze", "切片分析 Agent", "generator", 2,
        input_summary="在沙箱容器内用 Codex 切片并分析缩略图",
    ) as step:
        project = db.session.get(CodeProject, project_id)
        step.model_provider = "codex-cli"
        db.session.commit()

        def on_event(event: dict) -> None:
            """Translate the Codex CLI stream-json into AgentEvents (live timeline).

            Codex's event schema differs from Claude Code's, so this maps a
            best-effort subset and silently ignores anything unrecognized; the
            reliable signal is our own ``slice_phase`` sentinel.
            """
            etype = str(event.get("type") or "")
            if etype == "slice_phase":
                phase = event.get("phase") or ""
                labels = {
                    "analyze": "Codex 正在分析缩略图并生成图层树",
                    "crop": "按图层裁剪图片切片（Pillow）",
                    "validate": "校验产物完整性",
                }
                recorder.emit(
                    AgentEventType.TOOL_CALL, step_id=step.id,
                    message=labels.get(phase, f"阶段：{phase}"),
                    payload={"phase": phase},
                )
                return
            # Best-effort: surface command executions / file writes if present.
            item = event.get("item") if isinstance(event.get("item"), dict) else event
            item_type = str(item.get("type") or item.get("item_type") or "")
            if "command" in item_type or "exec" in item_type:
                cmd = item.get("command") or item.get("text") or ""
                recorder.emit(
                    AgentEventType.TOOL_CALL, step_id=step.id,
                    message=(f"命令：{str(cmd)[:80]}" if cmd else "命令执行"),
                    payload={"command": str(cmd)[:500]},
                )
            elif "file" in item_type or "patch" in item_type:
                path = item.get("path") or item.get("file") or ""
                recorder.emit(
                    AgentEventType.FILE_CREATED, step_id=step.id,
                    message=f"写入 {path}" if path else "写入文件",
                    payload={"file": str(path)},
                )

        result = service.slice_image(
            image_data_url=image_data_url,
            name=project.title or "Design",
            context_ledger=ledger.render_for_prompt(),
            style_prompt=project.style_prompt or "",
            on_event=on_event,
            is_cancelled=ctx.is_cancelled,
        )

        if result.get("error") == "cancelled":
            return cancel_result(project_id)
        # The service always synthesizes a previewable IR (single-image fallback),
        # so a hard failure here means the container couldn't run at all
        # (config/auth/docker) — runtime refunds it.
        if not result.get("success"):
            raise RuntimeError(f"切片分析失败：{result.get('error') or '未知错误'}")

        degraded_reason = result.get("degraded_reason")
        if result.get("degraded"):
            recorder.emit(
                AgentEventType.WARNING,
                level=AgentEventLevel.WARNING,
                step_id=step.id,
                message="切片未完全成功，已降级为整图导出（在 Figma 中为单张图片，不可逐元素编辑）",
                payload={"degraded_reason": degraded_reason},
            )

        n_slices = len(result.get("slices") or {})
        step.set_output(
            output_summary=f"切片分析完成：裁出 {n_slices} 张图片切片"
            + ("（降级为整图）" if result.get("degraded") else "，其余还原为可编辑图层") + "。",
            reasoning_summary="沙箱容器内 Codex 读取缩略图产出 Design IR，Pillow 按 IMAGE 节点 bbox 裁剪切片。",
            self_check=f"图片切片 {n_slices} 张；降级={degraded_reason or '无'}。",
            next_action="组装为 Figma 插件可消费的导出包并发布。",
        )
    completed += 1
    progress("publish")

    # --- Step 3: Publish (plugin payload artifact) ---------------------------
    with recorder.step(
        "fe_slice_publish", "发布 Agent", "publisher", 3,
        input_summary="组装并发布 Figma 可编辑切片导出包",
    ) as step:
        ir_dict = result.get("ir") or {}
        slices = result.get("slices") or {}
        payload = build_slice_payload(ir_dict, slices, name=project.title or "Design")

        node_count = _count_nodes(payload.get("root"))
        image_count = len(payload.get("images") or {})

        step.add_artifact(
            AgentArtifactType.JSON, "Figma 可编辑切片导出包",
            content_json=payload,
            filename="figma_slice_payload.json",
            domain_ref_type="code_figma_slice_payload", domain_ref_id=project_id,
        )
        # A debug copy of the raw IR (handy when a layer looks off). Stored inline
        # (content_json) — no on-disk copy needed.
        step.add_artifact(
            AgentArtifactType.JSON, "切片 Design IR（调试）",
            content_json=ir_dict,
            filename="slice_ir.json",
            domain_ref_type="code_figma_slice_ir", domain_ref_id=project_id,
        )

        step.set_context(snapshot={"injected_text": "", "ledger": ledger.to_dict()})
        step.set_output(
            output_summary=f"已发布 Figma 切片导出包：{node_count} 个图层、{image_count} 张内联图片。"
            "可在「导出到 Figma」选择「智能切片」获取配对码。",
            reasoning_summary="dict→DesignIR→规范化(剔除空节点/悬空图片引用/合成根框)→plugin payload，存为可被导出端点复用的 artifact。",
            self_check=f"图层 {node_count}；内联图片 {image_count}；降级={result.get('degraded_reason') or '无'}。",
            next_action="在 Figma 插件中导入并逐元素微调。",
        )
    completed += 1
    progress("done")

    return {
        "status": AgentRunStatus.COMPLETED,
        "resource_id": project_id,
        "extra_credits": 0,
    }


def _count_nodes(node) -> int:
    if not isinstance(node, dict):
        return 0
    return 1 + sum(_count_nodes(child) for child in node.get("children") or [])
