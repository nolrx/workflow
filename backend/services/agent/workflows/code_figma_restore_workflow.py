"""
Code domain workflow — ``code_figma_restore``.

Import a Figma design and restore it into a complete, fully-interactive
single-file HTML app. The design is pulled via the Figma REST API (PAT auth) as
both a precise node-tree IR and a rendered PNG; both are fed to the build model
(node tree as text + render as vision reference) for a high-fidelity
reconstruction, which is then adversarially reviewed (and repaired once).

The final ``index.html`` is published under ``domain_ref_type =
"code_frontend_html"`` — the SAME type the regular frontend workflow uses — so
the existing preview / download surface renders it with zero extra wiring.
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
from backend.models.code import CodeProject, FigmaCredential
from backend.services import pricing
from backend.services.agent.context_ledger import ContextLedger, seed_from_inputs
from backend.services.agent.context_verifier import (
    emit_context_events,
    gate_available,
    run_ai_consistency_gate,
    run_deterministic_checks,
)
from backend.services.code.figma.crypto import FigmaTokenDecryptError, decrypt_token
from backend.services.code.figma.ir import figma_node_to_ir
from backend.services.code.figma_service import (
    FigmaError,
    FigmaService,
    extract_first_frame_node,
    parse_figma_url,
)
from backend.services.code.frontend_build_service import get_frontend_build_service
from backend.services.credit_service import charge

logger = logging.getLogger(__name__)

TOTAL_STEPS = 5  # planner, fetch, build, critic, publish (repair is an uncounted extra)


def run_code_figma_restore_workflow(ctx, recorder) -> dict:
    """Pull a Figma design and restore it into a runnable single-file HTML app."""
    service = get_frontend_build_service()
    completed = 0
    extra_credits = 0
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
        return {
            "status": AgentRunStatus.CANCELLED,
            "resource_id": project_id,
            "extra_credits": extra_credits,
        }

    # --- Step 1: Planner (validate project + credential + parse target) ------
    with recorder.step(
        "figma_planner", "Figma 还原规划 Agent", "planner", 1,
        input_summary="校验项目与 Figma 凭据并解析目标文件",
    ) as step:
        if not ctx.resource_id:
            raise ValueError("缺少 resource_id：Figma 还原需要一个已有的 Code 项目")
        project = CodeProject.query.filter_by(id=ctx.resource_id, user_id=ctx.user_id).first()
        if not project:
            raise ValueError("项目不存在或无权访问")

        figma_url = (ctx.config.get("figma_url") or "").strip()
        if not figma_url:
            raise ValueError("缺少 figma_url：请提供要还原的 Figma 链接")

        credential = FigmaCredential.query.filter_by(user_id=ctx.user_id).first()
        if not credential:
            raise ValueError("尚未连接 Figma：请先在设置中粘贴 Figma 个人访问令牌")
        try:
            token = decrypt_token(credential.token_encrypted)
        except FigmaTokenDecryptError as exc:
            raise ValueError("Figma 凭据已失效，请重新粘贴个人访问令牌") from exc

        try:
            file_key, node_id = parse_figma_url(figma_url)
        except FigmaError as exc:
            raise ValueError(exc.message) from exc

        project_id = project.id

        # Reload the consensus ledger from this project's latest full-generation
        # run so terminology stays consistent; seed a baseline if there is none.
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

        step.add_artifact(
            AgentArtifactType.JSON,
            "Figma 还原计划",
            content_json={
                "project_id": project_id,
                "file_key": file_key,
                "node_id": node_id,
                "stack": "Single-file HTML + inline CSS + browser-native JavaScript",
                "fidelity": "node-tree + rendered-image dual input",
            },
            filename="figma_restore_plan.json",
        )
        step.set_output(
            output_summary=f"已校验项目「{project.title}」与 Figma 凭据，准备拉取设计。",
            reasoning_summary="确认项目归属、解密 Figma 令牌、从链接解析文件 key 与目标节点，并载入共识账本。",
            decision_notes="技术栈固定为单文件 HTML；用节点树 + 渲染图双输入以最大化还原保真度。",
            next_action="拉取 Figma 节点树与渲染图。",
        )
    completed += 1
    progress("fetch")

    # --- Step 2: Fetch the Figma node tree + a rendered image ----------------
    if ctx.is_cancelled():
        return cancel_result(project_id)
    render_png = None
    with recorder.step(
        "figma_fetch", "Figma 拉取 Agent", "collector", 2,
        input_summary="拉取节点树并渲染参照图",
    ) as step:
        figma = FigmaService(token)
        try:
            target_id, target_doc = _resolve_target(figma, file_key, node_id)
            design_ir = figma_node_to_ir(target_doc, file_name=project.title or "Figma design")
            render_png = _fetch_render(figma, file_key, target_id, recorder, step)
        except FigmaError as exc:
            # Surface a clean message; this step has produced no artifact yet, so a
            # failure here still triggers the runtime's auto-refund.
            raise ValueError(exc.message) from exc

        ir_dict = design_ir.to_dict()
        step.add_artifact(
            AgentArtifactType.JSON,
            "Figma 设计 IR",
            content_json=ir_dict,
            filename="figma_ir.json",
            domain_ref_type="code_figma_ir",
            domain_ref_id=project_id,
        )
        if render_png:
            artifact = step.add_artifact(
                AgentArtifactType.IMAGE,
                "Figma 渲染参照图",
                filename="figma_render.png",
                mime_type="image/png",
                write_file=True,
                content_bytes=render_png,
                domain_ref_type="code_figma_render",
                domain_ref_id=project_id,
            )
            artifact.preview_url = f"/api/agent/artifacts/{artifact.id}/file"
            db.session.commit()

        node_count = _count_nodes(design_ir.root)
        step.set_output(
            output_summary=f"已拉取 Figma 设计：{node_count} 个节点{'，含渲染参照图' if render_png else '（无渲染图，仅用节点树）'}。",
            reasoning_summary="把 Figma 节点树转成精确的 Design IR，并请求一张渲染 PNG 作为视觉参照。",
            self_check=f"节点数：{node_count}；渲染图：{'有' if render_png else '无'}；颜色 token：{len(design_ir.tokens.colors)}。",
            next_action="据此还原为单文件 HTML。",
        )
    completed += 1
    progress("build")

    # --- Step 3: Build (restore HTML from IR + render) -----------------------
    if ctx.is_cancelled():
        return cancel_result(project_id)
    with recorder.step(
        "figma_build", "前端还原 Agent", "generator", 3,
        input_summary="据 Figma 设计还原完整可交互前端",
    ) as step:
        project = db.session.get(CodeProject, project_id)
        injected = ledger.render_for_prompt()
        ir_for_prompt = design_ir.to_prompt_text()
        result = service.build_from_figma(
            requirement=project.requirement_input,
            ir_text=ir_for_prompt,
            render_png=render_png,
            style_prompt=project.style_prompt or "",
            context_ledger=injected,
            on_model_call=step.model_tracer(),
        )
        html = result["html"]

        build_summary = (
            f"{result.get('summary', '')}\n产物: index.html\n"
            f"HTML 字符数: {len(html)}；视觉输入: {'是' if result.get('used_vision') else '否'}"
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
            description="context verify @ figma_build",
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
            output_summary=f"已据 Figma 设计还原 index.html。{result.get('summary', '')}".strip(),
            reasoning_summary="以节点树 IR 为结构依据、渲染图为视觉目标，生成单文件 HTML 应用并对照账本做一致性校验。",
            self_check=f"产物 index.html；HTML 字符数：{len(html)}；{det['summary']}",
            next_action="审查可交互性与完整性。",
        )
    completed += 1
    progress("critic")

    # --- Step 4: Critic ------------------------------------------------------
    if ctx.is_cancelled():
        return cancel_result(project_id)
    verdict = {"passed": True, "issues": [], "summary": ""}
    with recorder.step(
        "figma_critic", "审查 Agent", "critic", 4,
        input_summary="复检所有组件是否可交互、还原是否完整",
    ) as step:
        verdict = service.critique_app(html, on_model_call=step.model_tracer())
        step.add_artifact(
            AgentArtifactType.JSON, "审查结果", content_json=verdict, filename="figma_review.json"
        )
        issues = verdict.get("issues") or []
        step.set_output(
            output_summary=("通过：所有控件可交互、还原完整。" if verdict.get("passed") else f"未通过，发现 {len(issues)} 处问题。"),
            reasoning_summary="逐项检查死按钮、未接线输入、占位/未实现与无法运行项。",
            self_check=verdict.get("summary") or "",
            next_action=("发布。" if verdict.get("passed") else "修复后发布。"),
        )
    completed += 1

    # --- Step 4b: Repair (only if the critic rejected) -----------------------
    if not verdict.get("passed") and verdict.get("issues"):
        if ctx.is_cancelled():
            return cancel_result(project_id)
        progress("repair")
        with recorder.step(
            "figma_repair", "修复 Agent", "generator", 5,
            input_summary="按审查意见修复 HTML 并补全功能",
        ) as step:
            repaired = service.repair_app(
                html, verdict["issues"], on_model_call=step.model_tracer()
            )
            html = repaired["html"]
            step.set_output(
                output_summary=f"已按 {len(verdict['issues'])} 处意见修复。{repaired.get('summary', '')}".strip(),
                reasoning_summary="针对审查问题重写 HTML/CSS/JavaScript，确保交互与还原完整。",
                next_action="发布。",
            )

    # --- Step 5: Publish -----------------------------------------------------
    progress("publish")
    with recorder.step(
        "figma_publish", "发布 Agent", "publisher", 6,
        input_summary="保存并发布可预览的 HTML 文件",
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
                "source": "figma_restore",
            },
            filename="frontend_html_meta.json",
            domain_ref_type="code_frontend_meta",
            domain_ref_id=project_id,
        )
        step.set_output(
            output_summary="index.html 已发布，可在右侧预览或下载。",
            reasoning_summary="把还原出的最终 HTML 存入 artifact 并写入本地文件存储，供 iframe 预览和下载。",
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


def _resolve_target(figma: FigmaService, file_key: str, node_id):
    """Return ``(node_id, node_document)`` for the frame to restore.

    If the URL carried a node id, fetch exactly that subtree. Otherwise read the
    file and pick the first frame on the first page (falling back to the page).
    """
    if node_id:
        nodes_response = figma.get_nodes(file_key, [node_id])
        return extract_first_frame_node(nodes_response, node_id)

    file_data = figma.get_file(file_key)
    document = file_data.get("document") if isinstance(file_data, dict) else None
    if not isinstance(document, dict):
        raise FigmaError(404, "NOT_FOUND", "Figma 文件内容为空")
    chosen = _pick_default_frame(document)
    if not chosen:
        raise FigmaError(404, "NOT_FOUND", "Figma 文件中没有可还原的画板")
    return chosen


def _pick_default_frame(document: dict):
    """Walk DOCUMENT -> CANVAS -> first FRAME; fall back to the first canvas."""
    for canvas in document.get("children") or []:
        if not isinstance(canvas, dict):
            continue
        for child in canvas.get("children") or []:
            if isinstance(child, dict) and (child.get("type") or "").upper() == "FRAME":
                return str(child.get("id") or ""), child
        # No frame on this canvas — use the canvas itself.
        return str(canvas.get("id") or ""), canvas
    return None


def _fetch_render(figma: FigmaService, file_key: str, node_id: str, recorder, step):
    """Fetch a rendered PNG for the node; degrade to None (warn) on any failure."""
    try:
        urls = figma.get_image_urls(file_key, [node_id], scale=2.0, fmt="png")
        url = urls.get(node_id)
        if not url:
            recorder.emit(
                AgentEventType.WARNING,
                level=AgentEventLevel.WARNING,
                step_id=step.id,
                message="Figma 未能渲染该节点，将仅用节点树还原",
            )
            return None
        return figma.download_image(url)
    except FigmaError as exc:
        recorder.emit(
            AgentEventType.WARNING,
            level=AgentEventLevel.WARNING,
            step_id=step.id,
            message=f"获取 Figma 渲染图失败（{exc.message}），将仅用节点树还原",
        )
        return None


def _count_nodes(node) -> int:
    return 1 + sum(_count_nodes(child) for child in node.children)
