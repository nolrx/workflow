"""
Code domain workflow — ``code_backend_project_generation``.

The backend counterpart of the frontend project workflow: an autonomous coding
CLI runs in a sandboxed container and produces a COMPLETE multi-file backend
project (polyglot — whatever the development flow chose) that implements the
SHARED OpenAPI contract and ships its own Dockerfile. It does NOT run the app
(the deploy run builds + runs the project's Dockerfile); it writes the code,
does a light syntax check, guarantees a Dockerfile, and publishes the source.

Steps: ``be_planner`` -> ``be_project_build`` -> ``be_publish`` (3 counted steps).
Runs CONCURRENTLY with ``code_frontend_project_generation`` and
``code_middleware_provisioning`` for the same project; all three read the same
frozen contract from ``CodeProjectLedger`` so they stay in lock-step.
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
from backend.models.code import CodeProject
from backend.services import pricing
from backend.services.agent.context_ledger import ContextLedger, seed_from_inputs
from backend.services.agent.context_verifier import gate_available
from backend.services.code.backend_project_service import get_backend_project_service
from backend.services.code.fullstack import contract_service
from backend.services.credit_service import charge

logger = logging.getLogger(__name__)

TOTAL_STEPS = 3
_MAX_DOC_CHARS = 1800
_MAX_DIGEST_CHARS = 12_000

_BE_PHASE_LABELS = {
    "reinforce": "二次功能补强:把 FR/NFR/M 锚点做成端到端可用",
    "detect": "识别后端技术栈",
    "validate": "语法静态自检(不安装依赖)",  # retained for replaying older runs
    "install": "安装依赖(真实构建)",
    "compile": "编译 / 类型检查",
    "test": "运行单元 / 契约测试",
    "package-verify": "验证可打包(原生制品 + Dockerfile 静态检查)",
    "ai-repair": "AI 定向修复构建 / 测试报错",
    "dockerfile": "校验 / 补齐 Dockerfile",
}


def _render_middleware(manifest: dict) -> str:
    """A compact, readable middleware block for the backend build prompt."""
    if not manifest:
        return ""
    lines = ["## 中间件清单(后端须读 env 接入,部署时由平台注入连接串)"]
    for store in manifest.get("datastores") or []:
        lines.append(f"- 数据存储:{store.get('type')} — {store.get('purpose', '')}")
    cache = manifest.get("cache")
    if cache:
        lines.append(f"- 缓存:{cache.get('type')} — {cache.get('purpose', '')}")
    queue = manifest.get("queue")
    if queue:
        lines.append(f"- 队列:{queue.get('type')} — {queue.get('purpose', '')}")
    env = manifest.get("env") or []
    if env:
        lines.append("- 运行环境变量(必须从环境读取,不要硬编码):")
        for e in env:
            lines.append(f"  - {e.get('name')}:{e.get('purpose', '')}")
    notes = manifest.get("notes")
    if notes:
        lines.append(f"- 备注:{notes}")
    return "\n".join(lines)


def _source_digest(files: dict, limit_total: int = 60_000, limit_file: int = 4000) -> str:
    parts: list[str] = []
    total = 0
    skip = ("node_modules/", ".git/", "dist/", "vendor/", "target/")
    for path in sorted(files):
        low = path.lower()
        if any(s in low for s in skip):
            continue
        raw = files[path]
        try:
            text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        except (UnicodeDecodeError, AttributeError):
            continue
        chunk = f"// ===== {path} =====\n{text[:limit_file]}"
        parts.append(chunk)
        total += len(chunk)
        if total >= limit_total:
            break
    return "\n\n".join(parts)[:limit_total]


def _documents_digest(project: CodeProject) -> str:
    return contract_service.backend_documents_digest(project) or _all_documents_digest(project)


def _all_documents_digest(project: CodeProject) -> str:
    parts = []
    for document in project.documents.all():
        body = (document.content or "")[:_MAX_DOC_CHARS]
        parts.append(f"## {document.title} ({document.document_type})\n{body}")
    return "\n\n".join(parts)[:_MAX_DIGEST_CHARS]


def _load_shared(project: CodeProject, user_id: str, team_id):
    """Load the frozen contract + manifest + seed ledger for this project.

    Lazily synthesizes (and persists) the contract if no row exists yet, so a BE
    run started directly (not via the fullstack endpoint) is still self-sufficient.
    """
    row = contract_service.get_ledger(project.id)
    if row is None or row.contract_status != "ready":
        row = contract_service.ensure_contract(project, user_id, team_id)
    contract = row.get_api_contract()
    manifest = row.get_middleware_manifest()
    ledger = ContextLedger.load(row.get_shared_ledger())
    if ledger.is_empty():
        ledger = seed_from_inputs(
            project.requirement_input, project.title, project.get_selected_style_ids()
        )
    return contract, manifest, ledger


def run_code_backend_project_workflow(ctx, recorder) -> dict:
    """Generate + publish a runnable multi-file backend project."""
    service = get_backend_project_service()
    completed = 0
    ledger = ContextLedger.empty()
    contract: dict = {}
    manifest: dict = {}
    # Set when the acceptance review judges the backend does NOT honor the shared
    # contract OR leaves a core functional anchor (FR/M) unimplemented (verdict=FAIL):
    # the run then completes as PARTIAL (still publishable + deployable, but flagged)
    # so a "only blows up at integration" contract/feature gap is caught here instead
    # of at deploy/runtime.
    contract_failed = False
    fr_coverage_summary: list = []  # per-anchor coverage from the review, for the meta

    def progress(current_step: str) -> None:
        run = db.session.get(AgentRun, ctx.run_id)
        if run:
            run.set_progress({
                "total_steps": TOTAL_STEPS, "completed_steps": completed,
                "failed_steps": 0, "current_step": current_step,
            })
            db.session.commit()
        recorder.emit(
            AgentEventType.PROGRESS, message=f"进度 {completed}/{TOTAL_STEPS}",
            payload={"completed": completed, "total": TOTAL_STEPS, "current": current_step},
        )

    def cancel_result(project_id) -> dict:
        recorder.emit(AgentEventType.WARNING, level=AgentEventLevel.WARNING,
                      message="收到取消请求，停止后续步骤")
        return {"status": AgentRunStatus.CANCELLED, "resource_id": project_id}

    # --- Step 1: Planner -----------------------------------------------------
    with recorder.step(
        "be_planner", "后端规划 Agent", "planner", 1, input_summary="校验项目并载入共享 API 契约"
    ) as step:
        if not ctx.resource_id:
            raise ValueError("缺少 resource_id：后端工程生成需要一个已有的 Code 项目")
        project = CodeProject.query.filter_by(id=ctx.resource_id, user_id=ctx.user_id).first()
        if not project:
            raise ValueError("项目不存在或无权访问")
        if not project.requirements_doc or not project.development_flow:
            raise ValueError("项目尚未完成需求文档与开发流程，无法生成后端")
        project_id = project.id

        contract, manifest, ledger = _load_shared(project, ctx.user_id, ctx.team_id)
        be_run = db.session.get(AgentRun, ctx.run_id)
        be_run.set_context_ledger(ledger.to_dict())
        db.session.commit()

        if not service.is_configured():
            recorder.emit(AgentEventType.WARNING, level=AgentEventLevel.WARNING, step_id=step.id,
                          message="未配置 ANTHROPIC_API_KEY，无法运行容器化后端生成")

        step.set_context(snapshot={"injected_text": "", "ledger": ledger.to_dict()})
        ts = (contract.get("tech_stack") or {})
        step.set_output(
            output_summary=f"已载入共享 API 契约（{ts.get('language', '?')}/{ts.get('framework', '?')}），准备生成后端工程。",
            reasoning_summary="读取由开发流程合成、三端共享的 OpenAPI 契约与中间件清单，确保后端实现与前端调用严格一致。",
            decision_notes="后端技术栈跟随开发流程「技术假设」；工程自带 Dockerfile，部署时由平台 docker build 并接入共享网络。",
            self_check=f"需求/流程齐备；契约端点已载入；中间件项 {len(manifest.get('datastores') or [])} 个。",
            next_action="在沙箱容器内按契约生成后端工程。",
        )
    completed += 1
    progress("build")

    # --- Step 2: Container build ---------------------------------------------
    if ctx.is_cancelled():
        return cancel_result(project_id)
    result: dict = {}
    with recorder.step(
        "be_project_build", "后端工程 Agent", "generator", 2,
        input_summary="在沙箱容器内按共享契约生成后端工程(含 Dockerfile)",
    ) as step:
        project = db.session.get(CodeProject, project_id)
        injected = ledger.render_for_prompt()
        contract_block = contract_service.render_contract_for_prompt(contract)
        middleware_block = _render_middleware(manifest)
        step.model_provider = "claude-code-cli"
        db.session.commit()

        def on_event(event: dict) -> None:
            etype = event.get("type")
            if etype == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    name = block.get("name") or ""
                    inp = block.get("input") or {}
                    if name in ("Write", "Edit"):
                        fpath = inp.get("file_path") or inp.get("path") or ""
                        recorder.emit(AgentEventType.FILE_CREATED, step_id=step.id,
                                      message=f"写入 {fpath}", payload={"tool": name, "file": fpath})
                    else:
                        cmd = inp.get("command") or ""
                        recorder.emit(AgentEventType.TOOL_CALL, step_id=step.id,
                                      message=(f"{name}: {cmd[:80]}" if cmd else name),
                                      payload={"tool": name, "command": cmd[:500]})
            elif etype == "user":
                for block in event.get("message", {}).get("content", []):
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    content = block.get("content")
                    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                    recorder.emit(AgentEventType.TOOL_RESULT, step_id=step.id,
                                  message="工具返回", payload={"output": (text or "")[:2000]})
            elif etype == "be_phase":
                phase = event.get("phase") or ""
                recorder.emit(AgentEventType.TOOL_CALL, step_id=step.id,
                              message=_BE_PHASE_LABELS.get(phase, f"构建阶段：{phase}"),
                              payload={"phase": phase})

        result = service.build_project(
            requirement=project.requirement_input,
            requirements_doc=project.requirements_doc,
            development_flow=project.development_flow or "",
            documents_digest=_documents_digest(project),
            contract_block=contract_block,
            middleware_block=middleware_block,
            context_ledger=injected,
            on_event=on_event,
            is_cancelled=ctx.is_cancelled,
        )

        if result.get("error") == "cancelled":
            return cancel_result(project_id)
        if not result.get("success"):
            raise RuntimeError(f"后端工程生成失败：{result.get('error') or '未知错误'}")

        usage = result.get("usage") or {}
        src_files = result.get("files") or {}
        n_src = len(src_files)
        stack = result.get("stack") or "unknown"
        dockerfile_origin = result.get("dockerfile_origin")
        if dockerfile_origin == "synthesized":
            recorder.emit(AgentEventType.WARNING, level=AgentEventLevel.WARNING, step_id=step.id,
                          message="Agent 未自带 Dockerfile，已按检测到的技术栈合成一个（可能需在部署阶段微调）。",
                          payload={"stack": stack})

        # Generation-time native build verdict: green / green-repaired publish
        # cleanly; a still-red build after the self-heal round(s) is published as
        # ``degraded`` (NOT a hard failure) — the source is still valuable and the
        # deploy run has a SECOND docker-build + AI-repair ladder as backstop.
        build_state = result.get("build_state") or "unknown"
        if result.get("degraded"):
            recorder.emit(
                AgentEventType.WARNING, level=AgentEventLevel.WARNING, step_id=step.id,
                message=(f"本机原生构建未完全通过（{result.get('degraded_reason') or build_state}，"
                         f"已自愈 {result.get('build_repaired_rounds') or 0} 轮）；源码已生成,"
                         "将在部署阶段再次 docker build + AI 修复。"),
                payload={"build_state": build_state,
                         "degraded_reason": result.get("degraded_reason"),
                         "repaired_rounds": result.get("build_repaired_rounds"),
                         "build_logs": result.get("build_logs"),
                         "tests_ran": result.get("tests_ran")},
            )
        else:
            recorder.emit(
                AgentEventType.PROGRESS, step_id=step.id,
                message=(f"本机原生构建验证通过（{build_state}）"
                         + ("，测试已运行" if result.get("tests_ran") else "")),
                payload={"build_state": build_state, "tests_ran": result.get("tests_ran"),
                         "scaffold": result.get("scaffold")},
            )
        if result.get("dockerfile_warn"):
            recorder.emit(AgentEventType.WARNING, level=AgentEventLevel.WARNING, step_id=step.id,
                          message=f"Dockerfile 静态检查提示：{result.get('dockerfile_warn')}",
                          payload={"dockerfile_warn": result.get("dockerfile_warn")})

        # --- Acceptance review vs the shared contract (advisory, charged) ----
        if src_files and gate_available() and charge(
            user_id=ctx.user_id, amount=pricing.CODE_CONTEXT_VERIFY,
            operation="code_context_verify", resource_type="agent_run",
            resource_id=ctx.run_id, description="backend acceptance review", team_id=ctx.team_id,
        ):
            review = service.review_project(
                source_digest=_source_digest(src_files),
                contract_summary=contract_block,
            )
            if review:
                verdict = str(review.get("verdict") or "").upper()
                missing_ep = [
                    c.get("endpoint") or c.get("id") for c in (review.get("endpoint_coverage") or [])
                    if isinstance(c, dict) and not c.get("covered")
                ]
                # Functional-anchor (FR/NFR/M) coverage — the "implement the whole
                # feature, not just the route" gate. The critic already folds
                # "core FR/M uncovered" into verdict=FAIL, so PARTIAL stays a single
                # source of truth (verdict); missing_fr is for narration + meta.
                fr_coverage_summary = (review.get("fr_coverage") or [])[:50]
                missing_fr = [
                    c.get("id") for c in fr_coverage_summary
                    if isinstance(c, dict) and not c.get("covered")
                ]
                missing_all = [m for m in (*missing_ep, *missing_fr) if m]
                concern = verdict in ("CONCERNS", "FAIL")
                contract_failed = verdict == "FAIL"
                recorder.emit(
                    AgentEventType.WARNING if concern else AgentEventType.PROGRESS,
                    level=AgentEventLevel.WARNING if concern else AgentEventLevel.INFO,
                    step_id=step.id,
                    message=f"契约 / 功能锚点符合性评审：{verdict or '—'}" + (f"；未覆盖 {', '.join(missing_all)}" if missing_all else "；端点与功能锚点覆盖完整"),
                    payload={"verdict": verdict, "missing_endpoints": missing_ep, "missing_fr": missing_fr,
                             "issues": (review.get("issues") or [])[:20], "summary": review.get("summary")},
                )
                step.add_artifact(
                    AgentArtifactType.JSON, "后端契约符合性评审", content_json=review,
                    filename="backend_project_review.json",
                    domain_ref_type="code_backend_project_meta", domain_ref_id=project_id,
                )

        step.model_response = (result.get("summary") or "")[:8000]
        db.session.commit()
        recorder.emit(AgentEventType.MODEL_RESPONSE, step_id=step.id, message="agent 完成",
                      payload={"summary": result.get("summary"), "usage": usage,
                               "cost_usd": result.get("cost_usd"), "stack": stack})
        step.set_output(
            output_summary=f"已生成后端工程：{n_src} 个文件，技术栈 {stack}，Dockerfile 来源 {dockerfile_origin}。{result.get('summary', '')}".strip(),
            reasoning_summary="沙箱容器内 Claude Code 按共享契约自主创建多文件后端工程，并补齐运行所需 Dockerfile 与 /health、env 读取约定;实际构建运行交给部署阶段。",
            self_check=f"源码 {n_src} 文件；栈={stack}；Dockerfile={dockerfile_origin}；cost≈${result.get('cost_usd')}",
            next_action="发布源码并等待原子部署。",
        )
    completed += 1
    progress("publish")

    # --- Step 3: Publish (source zip + meta) ---------------------------------
    with recorder.step(
        "be_publish", "发布 Agent", "publisher", 3, input_summary="保存后端源码 zip 与元数据"
    ) as step:
        files = result.get("files") or {}
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for rel, content in files.items():
                archive.writestr(rel, content or b"")
        step.add_artifact(
            AgentArtifactType.TEXT, "后端工程源码（zip）", filename="backend_project.zip",
            mime_type="application/zip", write_file=True, content_bytes=buffer.getvalue(),
            domain_ref_type="code_backend_project_zip", domain_ref_id=project_id,
        )
        step.add_artifact(
            AgentArtifactType.JSON, "后端工程元数据",
            content_json={
                "source_files": sorted(files.keys()),
                "stack": result.get("stack"),
                "dockerfile_origin": result.get("dockerfile_origin"),
                "cost_usd": result.get("cost_usd"),
                "usage": result.get("usage"),
                "summary": result.get("summary"),
                "delivery": "multi-file-backend-project",
                "deployable": True,
                # Generation-time CI signals (visible to the deploy run + the UI).
                "build_state": result.get("build_state"),
                "degraded": bool(result.get("degraded")),
                "degraded_reason": result.get("degraded_reason"),
                "build_repaired_rounds": result.get("build_repaired_rounds") or 0,
                "tests_ran": bool(result.get("tests_ran")),
                "scaffold": result.get("scaffold"),
                "contract_pass": not contract_failed,
                # Functional-anchor coverage + BMAD reinforce-pass diagnostics.
                "fr_coverage": fr_coverage_summary,
                "fr_uncovered": [
                    c.get("id") for c in fr_coverage_summary
                    if isinstance(c, dict) and not c.get("covered")
                ],
                "reinforce_state": result.get("reinforce_state"),
            },
            filename="backend_project_meta.json",
            domain_ref_type="code_backend_project_meta", domain_ref_id=project_id,
        )
        step.set_context(snapshot={"injected_text": "", "ledger": ledger.to_dict()})
        step.set_output(
            output_summary=(
                "后端工程已发布：可在部署阶段构建运行,或下载源码 zip。"
                + ("（契约符合性评审为 FAIL，已标记 PARTIAL）" if contract_failed else "")
            ),
            reasoning_summary="把后端源码打包为 zip artifact;部署阶段读取它 docker build 工程自带 Dockerfile 并接入共享网络。",
            self_check=(
                f"源码 {len(files)} 文件;栈={result.get('stack')};"
                f"构建={result.get('build_state')};契约={'FAIL' if contract_failed else 'ok'}"
            ),
            next_action="待三端就绪后触发原子部署。",
        )
    completed += 1
    progress("done")

    # PARTIAL (not COMPLETED) when the backend failed the contract review: the run
    # is still publishable + deployable (deploy's _BUILT set accepts PARTIAL), but
    # the drift is flagged for the operator / an optional REQUIRE_CONTRACT_PASS gate.
    status = AgentRunStatus.PARTIAL if contract_failed else AgentRunStatus.COMPLETED
    return {"status": status, "resource_id": project_id}
