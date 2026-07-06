"""
Backend Dev Mode turn workflow (``code_dev_backend_turn``).

The backend-lane twin of ``code_dev_turn``: one interactive turn against the
long-running BACKEND dev container (``dev-be-<pid>``, native hot-reload) instead of
the frontend Vite container. Same three bounded steps, same recorder / SSE / cancel
/ checklist machinery — only the target service (``dev_backend_service``), the
scaffold, the prompt, and the verify signal (contract-driven integration tests +
backend house rules + FR-keyed acceptance review) differ.

  1. dev_be_prepare — load project + backend dev session, ensure the container is up
     (seed from the last backend project source, else a minimal Express scaffold;
     provisions an ISOLATED dev database), reload the session ledger, seed checklist.
  2. dev_be_edit    — one ``docker exec claude -p`` edit round grounded in the shared
     OpenAPI contract (the source of truth for the FE/BE boundary). A change to the
     dev runner / deps / config triggers a container restart so it takes effect.
  3. dev_be_verify  — collect source, run backend house rules + the skeptical
     acceptance review (FR-keyed → checklist) + the contract-driven integration test
     against the LIVE dev backend, fold results onto the persistent board, snapshot
     the source (``code_backend_project_zip``) so re-entry restores the work.

Comments in English to match the Code/core convention.
"""
import logging
import os
from datetime import datetime

from backend.extensions import db
from backend.models.agent import (
    AgentArtifactType,
    AgentEventLevel,
    AgentEventType,
    AgentRun,
    AgentRunStatus,
)
from backend.models.code import CodeProject
from backend.models.code.fullstack import CodeDevSession, DevSessionStatus
from backend.services.agent.context_ledger import ContextLedger, seed_from_inputs
from backend.services.agent.workflows import _verify_support
from backend.services.agent.workflows.code_dev_turn_workflow import (
    _source_digest,
    _zip_source,
    seed_checklist,
    sync_checklist,
)
from backend.services.code import house_rules
from backend.services.code.backend_project_service import get_backend_project_service
from backend.services.code.dev_backend_service import (
    _MINIMAL_BACKEND_SCAFFOLD,
    get_dev_backend_service,
)

logger = logging.getLogger(__name__)

_LEDGER_RENDER_CHARS = int(os.getenv("DEV_MODE_LEDGER_CHARS", "4000"))
_DEV_REPAIR = os.getenv("CODE_DEV_TURN_REPAIR", "1") not in ("0", "false", "False", "")

# Files whose change requires a backend dev-server RESTART (read once at process
# start, or need a dependency install): the dev runner, dependency manifests /
# lockfiles across ecosystems, and env files. Source edits DON'T — those are picked
# up live by the native reloader (uvicorn/nodemon/flask). Framework-agnostic.
_BE_RESTART_TRIGGER_BASENAMES = frozenset({
    "dev-start.sh", "procfile", "procfile.dev",
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "requirements.txt", "pyproject.toml", "poetry.lock", "pipfile", "pipfile.lock",
    "go.mod", "go.sum", "pom.xml", "build.gradle", "build.gradle.kts",
    "dockerfile",
})


def _is_be_restart_trigger(fpath: str) -> bool:
    base = (fpath or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if base == ".env" or base.startswith(".env."):
        return True
    return base in _BE_RESTART_TRIGGER_BASENAMES


def _resolve_backend_source(project_id: str) -> dict:
    """The backend source to seed the dev container with: the last generated backend
    project when present, else the minimal runnable scaffold so the container ALWAYS
    has something serving on ``/health`` (the first turn fleshes it out)."""
    from backend.services.agent.workflows._iteration_support import load_prior_source

    try:
        src = load_prior_source(project_id, "backend")
    except Exception:  # noqa: BLE001
        src = {}
    return src or dict(_MINIMAL_BACKEND_SCAFFOLD)


def persist_backend_snapshot(step, project_id: str, files: dict) -> None:
    """Persist the backend dev container's source as a ``code_backend_project_zip``
    artifact so a LATER session (and deploy) restores the work — the container fs is
    destroyed on stop. Best-effort; never fails a turn."""
    if not files:
        return
    try:
        step.add_artifact(
            AgentArtifactType.TEXT, "后端开发模式源码快照（zip）",
            filename="dev_backend_snapshot.zip", mime_type="application/zip",
            write_file=True, content_bytes=_zip_source(files),
            domain_ref_type="code_backend_project_zip", domain_ref_id=project_id,
        )
    except Exception:  # noqa: BLE001
        logger.warning("dev backend source snapshot persist failed for %s", project_id, exc_info=True)


def persist_backend_snapshot_standalone(run_id: str, project_id: str, files: dict) -> bool:
    """Snapshot the backend dev source WITHOUT a recorder step (session stop / reap)."""
    if not files or not run_id:
        return False
    try:
        from backend.models.agent import AgentArtifact
        from backend.services.agent.files import save_artifact_file

        rel = save_artifact_file(run_id, None, "dev_backend_snapshot.zip", _zip_source(files))
        db.session.add(AgentArtifact(
            run_id=run_id, step_id=None, artifact_type=AgentArtifactType.TEXT,
            title="后端开发模式源码快照（zip）", filename="dev_backend_snapshot.zip",
            mime_type="application/zip", storage_path=rel,
            domain_ref_type="code_backend_project_zip", domain_ref_id=project_id,
        ))
        db.session.commit()
        return True
    except Exception:  # noqa: BLE001
        logger.warning("standalone dev backend snapshot persist failed for %s", project_id, exc_info=True)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False


def _load_dev_backend_ledger(session: CodeDevSession, project: CodeProject) -> ContextLedger:
    """Reload the consensus ledger session-first (multi-turn steering wins), then the
    latest backend dev turn, then the full-generation run, then a fresh seed."""
    sess_ledger = session.get_shared_ledger()
    if sess_ledger:
        led = ContextLedger.load(sess_ledger)
        if not led.is_empty():
            return led
    prior_turn = (
        AgentRun.query.filter_by(resource_id=project.id, workflow="code_dev_backend_turn")
        .filter(AgentRun.id != session.id)
        .order_by(AgentRun.created_at.desc())
        .first()
    )
    if prior_turn and prior_turn.get_context_ledger():
        led = ContextLedger.load(prior_turn.get_context_ledger())
        if not led.is_empty():
            return led
    full = (
        AgentRun.query.filter_by(resource_id=project.id, workflow="code_full_generation")
        .order_by(AgentRun.created_at.desc())
        .first()
    )
    led = ContextLedger.load(full.get_context_ledger() if full else None)
    if led.is_empty():
        led = seed_from_inputs(
            project.requirement_input, project.title, project.get_selected_style_ids()
        )
    return led


def _contract_blocks(project_id: str) -> tuple[str, str]:
    """(prompt_block, summary) rendered from the shared OpenAPI contract, if ready."""
    try:
        from backend.services.code.fullstack import contract_service

        row = contract_service.get_ledger(project_id)
        if row and row.contract_status == "ready":
            contract = row.get_api_contract()
            block = contract_service.render_contract_for_prompt(contract, include_db_schema=True)
            summary = contract_service.render_contract_for_prompt(contract, include_db_schema=False)
            return block or "", summary or ""
    except Exception:  # noqa: BLE001
        pass
    return "", ""


def _latest_frontend_run(project_id: str, user_id: str):
    """The newest built frontend run — the integration test reads it to learn what
    the frontend actually calls/parses. None → itest uses a contract-only plan."""
    return (
        AgentRun.query.filter_by(
            resource_id=project_id, user_id=user_id, workflow="code_frontend_project_generation"
        )
        .filter(AgentRun.status.in_([AgentRunStatus.COMPLETED, AgentRunStatus.PARTIAL]))
        .order_by(AgentRun.created_at.desc())
        .first()
    )


def _build_backend_turn_prompt(project, injected, contract_block, instruction, features, is_bootstrap):
    """Assemble the edit-mode instruction fed to the in-container claude (backend)."""
    feature_block = _verify_support.render_features_block(features)
    scaffold_note = (
        "本次为后端开发模式初始化:按共享 API 契约搭建后端工程骨架(路由/模型/健康检查),"
        "确保能在 $PORT 上启动并通过 /health;并写一个 dev-start.sh(用当前技术栈的热重载方式"
        "在 0.0.0.0:$PORT 启动,如 uvicorn --reload / nodemon / flask --debug);本轮只搭骨架,"
        "功能将在后续回合逐步实现。\n"
        if is_bootstrap else ""
    )
    return "\n\n".join(p for p in [
        "# 你在一个长运行的后端开发容器里,基于现有工程做增量修改(edit-mode)。"
        "后端以热重载方式运行,你的改动会被自动重载。共享 OpenAPI 契约是前后端边界的唯一真值源:"
        "实现/修改必须与契约一致(路径、方法、请求/响应字段、错误信封)。除非用户明确提出新需求,"
        "不要偏离既定契约与账本。",
        scaffold_note,
        f"# 需求文档(节选)\n{(project.requirements_doc or '')[:4000]}",
        f"# 开发流程(节选)\n{(project.development_flow or '')[:2000]}" if project.development_flow else "",
        f"# 共识账本(权威口径,按 FR/NFR/M 编号引用,不要改述)\n{injected}" if injected else "",
        contract_block or "",
        feature_block,
        f"# 本回合用户诉求(这是你这一轮要完成的具体改动)\n{instruction}" if instruction else "",
        "# 交付要求\n真实可用实现,禁止占位/TODO;必须监听环境变量 $PORT 且暴露 /health;"
        "保持 dev-start.sh 可用(热重载启动);遵守后端房规;改完确保服务能正常启动且既有接口不被破坏。",
    ] if p)


# --- workflow entry ----------------------------------------------------------
def run_code_dev_backend_turn_workflow(ctx, recorder) -> dict:
    """Entry point for the ``code_dev_backend_turn`` workflow (one backend dev turn)."""
    dev = get_dev_backend_service()
    service = get_backend_project_service()
    cfg = ctx.config or {}
    session_id = cfg.get("session_id")
    instruction = (cfg.get("instruction") or "").strip()
    is_bootstrap = bool(cfg.get("bootstrap"))
    is_audit = bool(cfg.get("audit"))
    run_tests = cfg.get("run_tests", True)
    total_steps = 3
    completed = 0

    def progress(current: str) -> None:
        run = db.session.get(AgentRun, ctx.run_id)
        if run:
            run.set_progress({
                "total_steps": total_steps, "completed_steps": completed,
                "failed_steps": 0, "current_step": current,
            })
            db.session.commit()

    def cancel_result(project_id) -> dict:
        recorder.emit(
            AgentEventType.WARNING, level=AgentEventLevel.WARNING,
            message="收到取消请求，已停止本回合后端开发",
        )
        return {"status": AgentRunStatus.CANCELLED, "resource_id": project_id}

    ledger = ContextLedger.empty()
    injected = ""
    contract_block = ""
    contract_summary = ""
    features: list[dict] = []

    # --- Step 1: prepare -----------------------------------------------------
    with recorder.step(
        "dev_be_prepare", "后端开发准备 Agent", "planner", 1,
        input_summary=(instruction[:200] or "初始化后端开发会话"),
    ) as step:
        if not ctx.resource_id:
            raise ValueError("缺少 resource_id：后端开发模式需要一个已有的 Code 项目")
        project = CodeProject.query.filter_by(id=ctx.resource_id, user_id=ctx.user_id).first()
        if not project:
            raise ValueError("项目不存在或无权访问")
        session = db.session.get(CodeDevSession, session_id) if session_id else None
        if not session or session.project_id != project.id or session.user_id != ctx.user_id:
            raise ValueError("后端开发会话不存在或无权访问")
        project_id = project.id

        if not dev.is_available():
            raise RuntimeError("后端开发模式不可用：未配置容器运行时或 Anthropic 凭证")

        ledger = _load_dev_backend_ledger(session, project)
        if instruction and not is_bootstrap:
            ledger.record_user_revision("dev-backend", instruction)
        run = db.session.get(AgentRun, ctx.run_id)
        run.set_context_ledger(ledger.to_dict())
        session.set_shared_ledger(ledger.to_dict())
        db.session.commit()

        seed_checklist(session.id, project_id, ledger.to_dict())
        features = _verify_support.features_from_ledger(ledger.to_dict())

        status = dev.container_status(project_id)
        if not status.get("running"):
            session.status = DevSessionStatus.STARTING
            db.session.commit()
            recorder.emit(
                AgentEventType.PROGRESS, step_id=step.id,
                message="正在启动长运行后端开发容器(装依赖 + 热重载启动 + 隔离 dev 数据库)…",
            )
            source = _resolve_backend_source(project_id)
            ok, err, info = dev.start_container(project_id, source)
            if not ok:
                session.status = DevSessionStatus.FAILED
                session.error_message = err
                db.session.commit()
                raise RuntimeError(f"后端开发容器启动失败：{err}")
            session.container_name = info.get("container_name")
            session.internal_port = info.get("internal_port")
            session.workdir = info.get("workdir")
            session.preview_path = f"/preview/{project_id}/api"
            session.base_source_run_id = None
            db.session.commit()

        session.status = DevSessionStatus.RUNNING
        # Give a freshly-started container a moment to install deps + boot.
        session.health = "healthy" if dev.wait_ready(project_id, timeout=60) else "unknown"
        session.last_active_at = datetime.utcnow()
        db.session.commit()

        recorder.emit(
            AgentEventType.DEV_PREVIEW_READY, step_id=step.id,
            message="后端开发容器就绪，前端可通过 /preview/<pid>/api 联调",
            payload={"api_base": session.preview_path, "container": session.container_name, "lane": "backend"},
        )
        step.set_output(
            output_summary=f"后端开发会话就绪：{project.title}",
            reasoning_summary="载入会话共识账本并核对功能清单;后端以热重载方式运行,契约为边界真值源。",
            self_check=f"容器运行中；功能清单 {len(features)} 项。",
        )
    completed = 1
    progress("edit")
    if ctx.is_cancelled():
        return cancel_result(project_id)

    # --- Step 2: edit --------------------------------------------------------
    edit_ok = True
    contract_block, contract_summary = _contract_blocks(project_id)
    if instruction:
        with recorder.step(
            "dev_be_edit", "后端开发 Agent", "generator", 2,
            input_summary="容器内 claude 增量编辑后端(edit-mode)",
        ) as step:
            injected = ledger.render_for_prompt(max_chars=_LEDGER_RENDER_CHARS)
            step.model_provider = "claude-code-cli (docker exec)"
            db.session.commit()

            config_touched = [False]

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
                            if _is_be_restart_trigger(fpath):
                                config_touched[0] = True
                            recorder.emit(
                                AgentEventType.FILE_CREATED, step_id=step.id,
                                message=f"写入 {fpath}", payload={"tool": name, "file": fpath},
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
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            content = block.get("content")
                            text = content if isinstance(content, str) else str(content)
                            recorder.emit(
                                AgentEventType.TOOL_RESULT, step_id=step.id,
                                message="工具返回", payload={"output": (text or "")[:2000]},
                            )

            prompt = _build_backend_turn_prompt(
                project, injected, contract_block, instruction, features, is_bootstrap)
            res = dev.exec_turn(project_id, prompt, on_event=on_event, is_cancelled=ctx.is_cancelled)
            if res.cancelled:
                return cancel_result(project_id)
            edit_ok = res.success
            if not res.success:
                recorder.emit(
                    AgentEventType.WARNING, level=AgentEventLevel.WARNING, step_id=step.id,
                    message=f"本回合编辑未干净完成（{res.error or '未知'}），将基于当前工作区验证。",
                )
            # A change to the dev runner / deps / config needs a restart to take effect
            # (the native reloader only watches SOURCE, not its own start config/deps).
            if config_touched[0] and not ctx.is_cancelled():
                recorder.emit(
                    AgentEventType.PROGRESS, step_id=step.id,
                    message="检测到依赖/启动配置变更,正在重启后端开发服务器(装依赖 + 重载)…",
                    payload={"restart": True},
                )
                if dev.restart_dev_server(project_id):
                    recorder.emit(
                        AgentEventType.DEV_PREVIEW_READY, step_id=step.id,
                        message="后端开发服务器已重启", payload={"restarted": True, "lane": "backend"},
                    )
                else:
                    recorder.emit(
                        AgentEventType.WARNING, level=AgentEventLevel.WARNING, step_id=step.id,
                        message="后端开发服务器重启后未就绪,请查看容器日志",
                    )
            step.set_output(
                output_summary="已在容器内完成一轮后端增量编辑（热重载已生效）。" if edit_ok
                else "编辑未完全成功，已尽力应用改动。",
            )
    else:
        recorder.emit(AgentEventType.PROGRESS, message="后端开发容器已就绪，等待你的第一条指令。")
    completed = 2
    progress("verify")
    if ctx.is_cancelled():
        return cancel_result(project_id)

    # --- Step 3: verify + checklist ------------------------------------------
    with recorder.step(
        "dev_be_verify", "后端验证与验收 Agent", "reviewer", 3,
        input_summary="后端房规 + 验收评审 + 契约集成测试 + 更新功能清单",
    ) as step:
        files = dev.collect_source(project_id)
        violations = house_rules.check_backend(files) if files else []
        review = None
        if files and (instruction or is_audit):
            try:
                review = service.review_project(
                    source_digest=_source_digest(files),
                    contract_summary=contract_summary,
                    requirements_doc=(project.requirements_doc or ""),
                    development_flow=(project.development_flow or ""),
                    features_block=_verify_support.render_features_block(features),
                    house_rules_report=house_rules.render_report(violations),
                    on_model_call=step.model_tracer() if hasattr(step, "model_tracer") else None,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("dev backend verify review raised: %s", exc)
                review = None

        feats, stats = _verify_support.apply_feature_results(
            features, (review or {}).get("feature_results")
        )
        verification = _verify_support.Verification(
            house_rule_errors=house_rules.errors(violations),
            house_rule_warnings=house_rules.warnings(violations),
            review=review, features=feats, feature_stats=stats,
        )

        # Contract-driven integration test against the LIVE dev backend (advisory:
        # informs the checklist + repair, never sinks the turn). Reuses the deploy
        # itest engine, pointed at the dev container.
        itest = None
        if run_tests and files and session.container_name and not ctx.is_cancelled():
            try:
                from backend.services.code.fullstack import integration_test_service

                itest = integration_test_service.run_integration_tests(
                    project_id=project_id, user_id=ctx.user_id, team_id=project.team_id,
                    container=session.container_name, port=session.internal_port or 8080,
                    frontend_run=_latest_frontend_run(project_id, ctx.user_id),
                    run_id=ctx.run_id, cancelled=ctx.is_cancelled,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("dev backend integration test raised: %s", exc)
                itest = None
            if itest:
                gate = itest.get("gate")
                summary = itest.get("summary") or {}
                recorder.emit(
                    AgentEventType.PROGRESS if gate != "fail" else AgentEventType.WARNING,
                    level=AgentEventLevel.WARNING if gate == "fail" else AgentEventLevel.INFO,
                    step_id=step.id,
                    message=(
                        f"契约集成测试:{gate}"
                        + (f"（{len(summary.get('failed') or [])} 个端点失败)" if gate == "fail" else "")
                    ),
                    payload={"itest": {"gate": gate, "summary": summary, "reason": itest.get("reason")}},
                )

        # One optional edit-mode repair round on blocking defects OR a failing itest.
        itest_failed = bool(itest and itest.get("gate") == "fail")
        if _DEV_REPAIR and (verification.blocking or itest_failed) and instruction and not ctx.is_cancelled():
            repair_extra = ""
            if itest_failed:
                try:
                    from backend.services.code.fullstack import integration_test_service

                    repair_extra = "\n\n# 集成测试失败(必须修复,契约为准)\n" + \
                        integration_test_service.format_failures_for_repair(
                            itest, contract_block=contract_summary,
                            logs=dev.container_logs(project_id, tail=120))
                except Exception:  # noqa: BLE001
                    repair_extra = ""
            recorder.emit(
                AgentEventType.WARNING, level=AgentEventLevel.WARNING, step_id=step.id,
                message=f"验证发现阻断问题({verification.summary_line()})，启动一次定向修复。",
                payload={"blocking": True, "feature_stats": stats, "itest_failed": itest_failed},
            )
            repaired = dev.exec_turn(
                project_id,
                "# 定向修复(edit-mode,只修下述问题,勿重写已通过功能;必须监听 $PORT + /health)\n"
                + verification.repair_instruction() + repair_extra,
                is_cancelled=ctx.is_cancelled,
            )
            if repaired.cancelled:
                return cancel_result(project_id)
            dev.restart_dev_server(project_id)
            files = dev.collect_source(project_id) or files
            violations = house_rules.check_backend(files) if files else violations
            if files and instruction:
                try:
                    review = service.review_project(
                        source_digest=_source_digest(files),
                        contract_summary=contract_summary,
                        requirements_doc=(project.requirements_doc or ""),
                        development_flow=(project.development_flow or ""),
                        features_block=_verify_support.render_features_block(features),
                        house_rules_report=house_rules.render_report(violations),
                    ) or review
                except Exception:  # noqa: BLE001
                    pass
            feats, stats = _verify_support.apply_feature_results(features, (review or {}).get("feature_results"))
            verification = _verify_support.Verification(
                house_rule_errors=house_rules.errors(violations),
                house_rule_warnings=house_rules.warnings(violations),
                review=review, features=feats, feature_stats=stats,
            )

        if instruction:
            persist_backend_snapshot(step, project_id, files)

        board = sync_checklist(session.id, project_id, feats, ctx.run_id)
        _progress = f"{board['functional_done']}/{board['functional_total']}"
        recorder.emit(
            AgentEventType.CHECKLIST_UPDATED, step_id=step.id,
            message=(
                f"已按现有后端代码校准功能清单:{_progress} 已实现"
                if is_audit and not instruction else f"后端功能进度 {_progress}"
            ),
            payload={"board": board, "feature_stats": stats, "lane": "backend",
                     "audit": bool(is_audit and not instruction)},
        )

        session.set_shared_ledger(ledger.to_dict())
        session.last_active_at = datetime.utcnow()
        db.session.commit()

        record = verification.to_record()
        if itest:
            record["integration_test"] = {"gate": itest.get("gate"), "reason": itest.get("reason"),
                                          "summary": itest.get("summary")}
        step.add_artifact(
            "json", "本回合后端验证结果", content_json=record,
            domain_ref_type="code_dev_backend_turn", domain_ref_id=session.id,
        )
        step.set_output(
            output_summary=f"验证:{verification.summary_line()}"
            + (f" · 集成测试 {itest.get('gate')}" if itest else ""),
            self_check=f"功能清单 {board['functional_done']}/{board['functional_total']} 通过。",
        )
    completed = 3
    progress("done")
    return {"status": AgentRunStatus.COMPLETED, "resource_id": project_id}
