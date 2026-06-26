"""
Canvas node executors.

Each executable canvas node type (agent / merge / branch) has a small executor
that turns its wired-in upstream outputs into a single text output, reusing the
existing recorder step handle for live token streaming + prompt/response tracing.
Source nodes are not executed here — their content is pre-filled by the workflow.

Executors raise on failure; the workflow loop turns that into a failed step and
prunes the node's downstream subgraph.
"""
import logging
import re
from dataclasses import dataclass

from backend.extensions import db
from backend.models.agent import AgentArtifactType, AgentEventLevel, AgentEventType
from backend.services.agent.dag_engine import CanvasNodeView, NodeResult
from backend.services.ai.factory import build_text_provider
from backend.services.prompt_library import compose_recipe_prompt, compose_system_prompt
from backend.services.prompts import prompt_store

logger = logging.getLogger(__name__)

# A labeled-input pair: (upstream node label, its output text). Used by the
# freeform (agent / merge / branch) executors, which work purely on text.
Inputs = list[tuple[str, str]]


@dataclass
class PortInput:
    """One resolved input to a typed stage node.

    Typed stage nodes resolve inputs *by port name* (the edge's targetHandle),
    each carrying the upstream's typed PortValue ``value`` (a doc / artifact
    reference) when the upstream is itself a typed port, plus ``text`` for prompt
    building / dereferenced content. ``value`` is None when fed by a freeform or
    source node (text-only).
    """

    port: str
    text: str
    value: dict | None = None
    label: str = ""


def _join_inputs(inputs: Inputs, *, labeled: bool, separator: str, title_template: str) -> str:
    """Concatenate upstream outputs, optionally prefixing each with its label."""
    parts: list[str] = []
    for label, text in inputs:
        body = (text or "").strip()
        if not body:
            continue
        if labeled:
            try:
                heading = title_template.format(label=label)
            except (KeyError, IndexError, ValueError):
                heading = f"## {label}"
            parts.append(f"{heading}\n{body}")
        else:
            parts.append(body)
    return (separator or "\n\n").join(parts)


def _system_prefix(config: dict) -> str:
    """Build the role/recipe system prefix for an agent or branch node."""
    recipe_id = config.get("recipe_id")
    role_ids = config.get("role_ids") or []
    if recipe_id:
        try:
            return compose_recipe_prompt(recipe_id)
        except ValueError:
            logger.warning("Unknown recipe_id on canvas node: %s", recipe_id)
    if role_ids:
        try:
            return compose_system_prompt(role_ids[0], role_ids[1:])
        except ValueError:
            logger.warning("Unknown role_ids on canvas node: %s", role_ids)
    return ""


def _model_kwargs(config: dict) -> dict:
    """Extract per-node text model overrides (provider/model/base_url)."""
    model = config.get("model") or {}
    return {
        "provider": model.get("provider"),
        "model": model.get("model_name"),
        "base_url": model.get("base_url"),
    }


def _stream_text(provider, prompt: str, step) -> str:
    """Stream a generation through the step's live tracer, returning full text."""
    tracer = step.model_tracer()
    on_delta = step.model_delta_tracer()
    full = ""
    try:
        for piece in provider.generate_text_stream(prompt):
            if piece:
                on_delta(piece)
                full += piece
    except Exception as exc:  # noqa: BLE001 - convert to a node failure
        tracer(
            prompt=prompt, text="", success=False, error=str(exc),
            provider=provider.provider_name, model=provider.model,
        )
        raise RuntimeError(f"模型调用失败: {exc}") from exc
    text = full.strip()
    if not text:
        tracer(
            prompt=prompt, text="", success=False, error="empty",
            provider=provider.provider_name, model=provider.model,
        )
        raise RuntimeError("模型返回空内容")
    tracer(
        prompt=prompt, text=text, success=True,
        provider=provider.provider_name, model=provider.model,
    )
    return text


def run_agent_node(
    node: CanvasNodeView, inputs: Inputs, *, injected_ledger: str, step
) -> NodeResult:
    """Free-prompt LLM node: combine wired inputs + ledger + prompt, then generate."""
    config = node.config or {}
    user_prompt = (config.get("prompt") or "").strip()
    join_mode = config.get("input_join") or "labeled"
    combined = _join_inputs(
        inputs, labeled=(join_mode == "labeled"), separator="\n\n", title_template="## {label}"
    )

    provider = build_text_provider(**_model_kwargs(config))
    if provider is None:
        raise RuntimeError("未配置可用的文本模型（claude / gemini）")

    sections = []
    prefix = _system_prefix(config)
    if prefix:
        sections.append(prefix)
    if injected_ledger:
        sections.append(injected_ledger)
    if combined:
        sections.append(f"# 上游输入\n{combined}")
    sections.append(f"# 本节点任务\n{user_prompt or '基于上游输入产出结论。'}")
    prompt = "\n\n".join(sections)

    text = _stream_text(provider, prompt, step)
    return NodeResult(
        output_text=text,
        output_summary=f"已产出结论（{len(text)} 字符）。",
        reasoning_summary="按所选角色与模型，结合连入的上游文档与项目共识账本生成结论。",
        self_check=f"上游输入 {len(inputs)} 项；模型 {provider.provider_name}/{provider.model}。",
    )


def run_merge_node(node: CanvasNodeView, inputs: Inputs, *, step) -> NodeResult:
    """Concatenate all wired upstream outputs (no LLM call)."""
    config = node.config or {}
    merged = _join_inputs(
        inputs,
        labeled=bool(config.get("labeled", True)),
        separator=config.get("separator") or "\n\n---\n\n",
        title_template=config.get("title_template") or "## {label}",
    )
    return NodeResult(
        output_text=merged,
        output_summary=f"已合并 {len(inputs)} 个输入（{len(merged)} 字符）。",
        reasoning_summary="把连入的多份内容按顺序拼接，供下游节点统一消费。",
    )


def _classify_keyword(text: str, branches: list[dict], default_branch: str) -> tuple[str, str]:
    """Pick the first branch whose any keyword appears in the text."""
    lowered = (text or "").lower()
    for branch in branches:
        for kw in branch.get("keywords") or []:
            if kw and kw.lower() in lowered:
                return branch.get("key"), branch.get("label") or branch.get("key")
    return default_branch, _label_for(branches, default_branch)


def _label_for(branches: list[dict], key: str) -> str:
    for branch in branches:
        if branch.get("key") == key:
            return branch.get("label") or key
    return key


def run_branch_node(
    node: CanvasNodeView, inputs: Inputs, *, injected_ledger: str, step
) -> NodeResult:
    """Route downstream by classifying the combined upstream output into one branch."""
    config = node.config or {}
    branches = config.get("branches") or []
    if not branches:
        raise RuntimeError("条件分支节点未配置任何分支")
    keys = [b.get("key") for b in branches if b.get("key")]
    default_branch = config.get("default_branch") or keys[0]
    combined = _join_inputs(inputs, labeled=True, separator="\n\n", title_template="## {label}")

    mode = config.get("mode") or "llm_classify"
    if mode == "keyword":
        selected, selected_label = _classify_keyword(combined, branches, default_branch)
    else:
        provider = build_text_provider(**_model_kwargs(config))
        if provider is None:
            raise RuntimeError("未配置可用的文本模型（claude / gemini）")
        options = "\n".join(f"- {b.get('key')}: {b.get('label')}" for b in branches)
        instruction = config.get("prompt") or "判断上游结论属于以下哪一类，只回类别名。"
        prompt = (
            f"{instruction}\n\n# 可选类别（只回其中一个 key）\n{options}\n\n"
            f"# 上游内容\n{combined or '（无）'}\n\n只输出一个 key，不要其他内容。"
        )
        raw = _stream_text(provider, prompt, step)
        selected = _match_branch(raw, keys, default_branch)
        selected_label = _label_for(branches, selected)

    return NodeResult(
        output_text=combined,
        active_handles={selected},
        output_summary=f"选择分支：{selected_label}",
        reasoning_summary="根据上游结论判定走向，仅激活选中分支，其余下游子图跳过。",
        extra={"selected_branch": selected},
    )


def _match_branch(raw: str, keys: list[str], default_branch: str) -> str:
    """Pick the branch key the model named (exact token match, else substring)."""
    lowered = (raw or "").lower()
    tokens = set(re.findall(r"[a-z0-9_\-]+", lowered))
    for key in keys:
        if key and key.lower() in tokens:
            return key
    for key in keys:
        if key and key.lower() in lowered:
            return key
    return default_branch


# --- typed stage nodes ---------------------------------------------------------
def resolve_prompt(contract, config: dict) -> str:
    """Resolve a stage's prompt: a frozen pin if the node carries one, else live HEAD.

    A published canvas stamps ``config.prompt_pin = {key, version, hash}`` onto the
    node; we then read the exact pinned body so a later prompt edit can't change
    what this saved canvas runs. A draft node (no pin) follows the live HEAD.
    """
    ref = getattr(contract, "prompt_ref", None)
    if ref is None:
        return ""
    pin = (config or {}).get("prompt_pin") or {}
    if pin.get("key") == ref.key and pin.get("hash"):
        return prompt_store.get_pinned(ref.key, pin["hash"])
    return prompt_store.get(ref.key)


def _ledger_text_inputs(port_inputs: list[PortInput]) -> list[tuple[str, str]]:
    """Convert typed port inputs to (label-by-port, text) pairs for prompt building."""
    return [(pi.port or pi.label, pi.text) for pi in port_inputs]


def run_stage_text_node(
    node: CanvasNodeView,
    port_inputs: list[PortInput],
    *,
    contract,
    injected_ledger,
    step,
    revise_instruction: str = "",
):
    """Typed, prompt-pinned text stage node.

    A reproducible upgrade of the freeform agent node: it runs the contract's
    PINNED prompt over the typed upstream inputs (resolved BY PORT NAME, each
    carrying a doc/artifact reference) + the consensus ledger, instead of a
    user-typed free prompt over concatenated text.
    """
    config = node.config or {}
    base_prompt = resolve_prompt(contract, config)

    provider = build_text_provider(**_model_kwargs(config))
    if provider is None:
        raise RuntimeError("未配置可用的文本模型（claude / gemini）")

    combined = _join_inputs(
        _ledger_text_inputs(port_inputs), labeled=True, separator="\n\n", title_template="## {label}"
    )
    sections = []
    if base_prompt:
        sections.append(base_prompt)
    if injected_ledger:
        sections.append(injected_ledger)
    if combined:
        sections.append(f"# 上游输入（按端口）\n{combined}")
    note = (config.get("prompt") or "").strip()
    if note:
        sections.append(f"# 额外说明\n{note}")
    if revise_instruction.strip():
        sections.append(f"# 用户调整意见（请据此修订本阶段产物）\n{revise_instruction.strip()}")
    prompt = "\n\n".join(sections)

    text = _stream_text(provider, prompt, step)
    return NodeResult(
        output_text=text,
        output_summary=f"已执行阶段「{contract.node_type}」（{len(text)} 字符）。",
        reasoning_summary=f"按契约「{contract.node_type}」的钉定提示词 + 按端口解析的 typed 输入 + 共识账本生成。",
        self_check=f"输入端口 {len(port_inputs)} 个;模型 {provider.provider_name}/{provider.model}。",
    )


def run_stage_preview_node(node: CanvasNodeView, port_inputs: list[PortInput], *, step):
    """Typed UI-preview stage node: generate preview thumbnails from upstream style.

    Reuses the code generation service's image pipeline (honours AI_IMAGE_PROVIDER);
    each thumbnail is stored as a disk-backed IMAGE artifact on this node. Returns
    the artifact ids in ``extra`` so the loop can emit a typed ``ui_preview``
    PortValue referencing them.
    """
    from backend.services.code import get_code_generation_service

    style_text = "\n\n".join(pi.text.strip() for pi in port_inputs if pi.text.strip())
    if not style_text:
        raise RuntimeError("缺少风格输入,无法生成 UI 预览图")

    artifact_ids: list[str] = []

    def _persist(index: int, image: dict, image_bytes: bytes) -> None:
        title = image.get("id") or f"预览图-{index + 1}"
        artifact = step.add_artifact(
            AgentArtifactType.IMAGE,
            title,
            filename=f"{title}.png",
            mime_type="image/png",
            write_file=True,
            content_bytes=image_bytes,
            domain_ref_type="code_canvas_node",
            domain_ref_id=node.id,
        )
        artifact.preview_url = f"/api/agent/artifacts/{artifact.id}/file"
        db.session.commit()
        artifact_ids.append(artifact.id)

    images = get_code_generation_service().generate_preview_images(
        style_text, count=2, on_image=_persist
    )
    return NodeResult(
        output_text=f"已生成 {len(images)} 张 UI 预览缩略图。",
        output_summary=f"已生成 {len(images)} 张 UI 预览图。",
        reasoning_summary="基于上游风格文档调用图像模型生成 UI 预览缩略图。",
        self_check=f"产出 {len(images)} 张图像 artifact。",
        extra={"image_artifact_ids": artifact_ids},
    )


def run_stage_deploy_node(node: CanvasNodeView, port_inputs: list[PortInput], *, project, ctx, recorder, step):
    """Typed deploy stage node: bring up the project's backend behind /app/<pid>/api.

    A thin wrapper over ``deploy_service.deploy`` — the SAME engine the linear
    full-stack deploy uses: provision DB → build the backend image → run the
    container → health-check → register the reverse proxy, with ordered rollback
    on failure. The frontend is served separately at ``/preview/<pid>/`` (not a
    deploy input). Deploys the project's CURRENT backend, so it powers the
    "I already have a backend — (re)deploy it" case directly. Narrates phases onto
    the step and emits a ``code:deployment`` reference.
    """
    from backend.services.code import deploy_service

    def on_phase(phase: str, message: str, payload: dict) -> None:
        recovery = phase in ("rollback",)
        recorder.emit(
            AgentEventType.WARNING if recovery else AgentEventType.PROGRESS,
            level=AgentEventLevel.WARNING if recovery else AgentEventLevel.INFO,
            step_id=step.id,
            message=message,
            payload={"phase": phase, **(payload or {})},
        )

    result = deploy_service.deploy(
        project,
        ctx.user_id,
        ctx.team_id,
        on_phase=on_phase,
        is_cancelled=ctx.is_cancelled,
        run_id=ctx.run_id,
    )
    if result.get("status") == deploy_service.DeploymentStatus.STOPPED:
        raise RuntimeError("部署已取消并回滚")
    if not result.get("success"):
        raise RuntimeError(f"应用部署失败(已回滚):{result.get('error') or '未知错误'}")

    dep = deploy_service.get_deployment(project.id)
    if dep:
        dep.deploy_run_id = ctx.run_id
        db.session.commit()

    preview_url = result.get("preview_url") or f"/preview/{project.id}/"
    api_base = result.get("api_base")
    step.add_artifact(
        AgentArtifactType.JSON,
        "部署元数据",
        content_json={
            "preview_url": preview_url,
            "api_base": api_base,
            "container": result.get("container"),
            "image": result.get("image"),
            "status": result.get("status"),
            "delivery": "canvas-deploy",
        },
        filename="deploy_meta.json",
        preview_url=preview_url,
        domain_ref_type="code_canvas_node",
        domain_ref_id=node.id,
    )
    return NodeResult(
        output_text=f"已部署后端并接入反代:{api_base};预览 {preview_url}。",
        output_summary=f"应用已部署({api_base})。",
        reasoning_summary="复用 deploy_service:建库 → 构建后端镜像 → 启动容器 → 健康检查 → 注册反代,失败有序回滚。",
        self_check=f"容器 {result.get('container')};镜像 {result.get('image')}。",
        extra={"deployment_id": dep.id if dep else None},
    )


def run_stage_be_node(node: CanvasNodeView, port_inputs: list[PortInput], *, project, ctx, recorder, step):
    """Typed backend-build stage node: generate a runnable multi-file backend project
    in the be-agent sandbox against the project's shared OpenAPI contract.

    Reuses the SAME ``backend_project_service`` the linear backend workflow uses, and
    publishes the source zip with the SAME ``domain_ref`` (``code_backend_project_zip``
    / ``project_id``) so a downstream deploy node picks it up via the deploy_service
    fallback. Heavy: shells out to the be-agent container (minutes).
    """
    import io
    import zipfile

    from backend.services.agent.workflows.code_backend_project_workflow import (
        _documents_digest,
        _load_shared,
        _render_middleware,
    )
    from backend.services.code.backend_project_service import get_backend_project_service
    from backend.services.code.fullstack import contract_service

    if not project.requirements_doc or not project.development_flow:
        raise RuntimeError("项目尚未完成需求文档与开发流程,无法生成后端")

    service = get_backend_project_service()
    contract, manifest, ledger = _load_shared(project, ctx.user_id, ctx.team_id)
    contract_block = contract_service.render_contract_for_prompt(contract)
    middleware_block = _render_middleware(manifest)

    def on_event(event: dict) -> None:
        if event.get("type") == "be_phase":
            recorder.emit(
                AgentEventType.TOOL_CALL,
                step_id=step.id,
                message=f"构建阶段:{event.get('phase')}",
                payload={"phase": event.get("phase")},
            )

    result = service.build_project(
        requirement=project.requirement_input,
        requirements_doc=project.requirements_doc,
        development_flow=project.development_flow or "",
        documents_digest=_documents_digest(project),
        contract_block=contract_block,
        middleware_block=middleware_block,
        context_ledger=ledger.render_for_prompt(),
        on_event=on_event,
        is_cancelled=ctx.is_cancelled,
    )
    if result.get("error") == "cancelled":
        raise RuntimeError("已取消")
    if not result.get("success"):
        raise RuntimeError(f"后端工程生成失败:{result.get('error') or '未知错误'}")

    files = result.get("files") or {}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for rel, content in files.items():
            archive.writestr(rel, content or b"")
    # SAME domain_ref as the linear backend publish → deploy node finds it.
    artifact = step.add_artifact(
        AgentArtifactType.TEXT,
        "后端工程源码(zip)",
        filename="backend_project.zip",
        mime_type="application/zip",
        write_file=True,
        content_bytes=buffer.getvalue(),
        domain_ref_type="code_backend_project_zip",
        domain_ref_id=project.id,
    )
    stack = result.get("stack") or "unknown"
    return NodeResult(
        output_text=f"已生成后端工程:{len(files)} 文件,技术栈 {stack}。",
        output_summary=f"后端工程已生成({len(files)} 文件,{stack})。",
        reasoning_summary="沙箱容器内按共享契约自主生成多文件后端工程(含 Dockerfile);实际构建运行交给 deploy 节点。",
        self_check=f"源码 {len(files)} 文件;栈={stack};构建={result.get('build_state')}。",
        extra={"backend_artifact_id": artifact.id},
    )


def run_stage_fe_node(node: CanvasNodeView, port_inputs: list[PortInput], *, project, ctx, recorder, step, injected_ledger=""):
    """Typed frontend-build stage node: generate a runnable multi-file React/Vite/TS
    project + previewable dist in the fe-agent sandbox. Reuses the SAME
    frontend_project_service; publishes the source zip with the linear domain_ref and
    a run-scoped preview (``/api/agent/runs/<run_id>/site/index.html``)."""
    import io
    import zipfile

    from backend.services.agent.files import agent_run_dir
    from backend.services.agent.workflows.code_frontend_project_workflow import _documents_digest
    from backend.services.code.frontend_project_service import get_frontend_project_service
    from backend.services.code.fullstack import contract_service

    if not project.requirements_doc or not project.development_flow:
        raise RuntimeError("项目尚未完成需求文档与开发流程,无法生成前端")

    service = get_frontend_project_service()
    contract_block = ""
    row = contract_service.get_ledger(project.id)
    if row and row.contract_status == "ready":
        contract_block = contract_service.render_contract_for_prompt(
            row.get_api_contract(), include_db_schema=False
        )

    result = service.build_project(
        requirement=project.requirement_input,
        requirements_doc=project.requirements_doc,
        development_flow=project.development_flow or "",
        documents_digest=_documents_digest(project),
        style_prompt=project.style_prompt or "",
        ui_baseline_prompt=project.ui_baseline_prompt or "",
        context_ledger=injected_ledger,
        contract_block=contract_block,
        on_event=lambda e: None,
        is_cancelled=ctx.is_cancelled,
    )
    if result.get("error") == "cancelled":
        raise RuntimeError("已取消")
    if not result.get("success"):
        raise RuntimeError(f"前端工程生成失败:{result.get('error') or '未知错误'}")

    files = result.get("files") or {}
    dist_files = result.get("dist_files") or {}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for rel, content in files.items():
            archive.writestr(rel, content or b"")
    artifact = step.add_artifact(
        AgentArtifactType.TEXT,
        "前端工程源码(zip)",
        filename="frontend_project.zip",
        mime_type="application/zip",
        write_file=True,
        content_bytes=buffer.getvalue(),
        domain_ref_type="code_frontend_project_zip",
        domain_ref_id=project.id,
    )
    # Built dist → run-scoped site dir for preview.
    site_dir = agent_run_dir(ctx.run_id) / "site"
    site_dir.mkdir(parents=True, exist_ok=True)
    for rel, content in dist_files.items():
        target = site_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content or b"")
    preview_url = f"/api/agent/runs/{ctx.run_id}/site/index.html"
    step.add_artifact(
        AgentArtifactType.JSON,
        "前端工程元数据",
        content_json={"preview_url": preview_url, "delivery": "canvas-frontend"},
        filename="frontend_project_meta.json",
        preview_url=preview_url,
        domain_ref_type="code_frontend_project_meta",
        domain_ref_id=project.id,
    )
    return NodeResult(
        output_text=f"已生成前端工程:{len(files)} 文件,dist {len(dist_files)} 文件;预览 {preview_url}。",
        output_summary=f"前端工程已生成({len(files)} 文件)。",
        reasoning_summary="沙箱容器内自主生成多文件 React/Vite/TS 工程并产出可预览 dist。",
        self_check=f"源码 {len(files)} 文件;dist {len(dist_files)} 文件;preview={preview_url}。",
        extra={"frontend_artifact_id": artifact.id},
    )


def run_stage_mw_node(node: CanvasNodeView, port_inputs: list[PortInput], *, project, ctx, recorder, step):
    """Typed middleware-provision stage node: generate the data layer (init.sql /
    migrations / seed) from the shared contract + manifest. Reuses the SAME
    middleware_service; publishes with the linear domain_refs (code_middleware_meta /
    code_middleware_sql) so the deploy node creates the DB + applies init.sql."""
    from backend.services.agent.workflows.code_middleware_workflow import _load_shared
    from backend.services.code import middleware_service
    from backend.services.code.fullstack import contract_service

    contract, manifest, _ledger = _load_shared(project, ctx.user_id, ctx.team_id)
    contract_summary = contract_service.render_contract_for_prompt(contract, max_chars=6000)
    data_layer = middleware_service.generate_data_layer(project, manifest, contract_summary)

    init_sql = data_layer.get("init_sql") or ""
    seed_sql = data_layer.get("seed_sql") or ""
    if init_sql.strip() or seed_sql.strip():
        combined = (init_sql + ("\n\n-- seed\n" + seed_sql if seed_sql.strip() else "")).strip()
        step.add_artifact(
            AgentArtifactType.TEXT,
            "初始化 SQL（init.sql）",
            filename="init.sql",
            mime_type="text/plain; charset=utf-8",
            write_file=True,
            content_text=combined,
            domain_ref_type="code_middleware_sql",
            domain_ref_id=project.id,
        )
    meta = step.add_artifact(
        AgentArtifactType.JSON,
        "中间件清单与数据层",
        content_json={
            "manifest": manifest,
            "entities": data_layer.get("entities") or [],
            "init_sql": init_sql,
            "seed_sql": seed_sql,
            "summary": data_layer.get("summary"),
            "delivery": "middleware-provisioning",
        },
        filename="middleware_meta.json",
        domain_ref_type="code_middleware_meta",
        domain_ref_id=project.id,
    )
    n = len(data_layer.get("entities") or [])
    return NodeResult(
        output_text=f"已生成中间件数据层:{n} 个实体,{'含' if init_sql.strip() else '无'} init.sql。",
        output_summary=f"中间件已生成({n} 实体)。",
        reasoning_summary="按共享契约+清单生成可移植 DDL/seed;部署 run 据此建库并应用。",
        self_check=f"实体 {n};init.sql {'有' if init_sql.strip() else '无'}。",
        extra={"middleware_artifact_id": meta.id},
    )
