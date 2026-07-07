"""
Dev Mode PARALLEL turn workflow (``code_dev_parallel_turn``).

Lets the user drive MULTIPLE subagents developing MULTIPLE features at once, safely.
The design-review hard constraint (合并地狱) is honored with the harness pattern from
``code-iterative-generation.md`` §2.3: parallelize ONLY on isolated worktrees, then a
MANDATORY integration barrier merges them back, with a serial re-apply fallback on
conflict/failure.

One bounded run, three steps:
  1. dev_prepare  — load project+session, ensure the dev container is up, reload the
     session-first ledger, fold each lane instruction in, reconcile the checklist.
  2. dev_parallel — create one git worktree per lane, FAN OUT one ``docker exec
     claude -p`` per worktree (concurrent), then the integration barrier: commit +
     merge each lane branch into /work; conflicting/failed lanes are re-applied
     SERIALLY onto the merged tree. Degrades to fully serial when git is unavailable.
  3. dev_verify   — collect the merged /work source, run house-rules + the acceptance
     review, fold onto the persistent checklist (atomic), emit CHECKLIST_UPDATED.

Concurrency safety mirrors ``_verify_support.run_reviewers``: the fan-out worker
threads do PURE subprocess (``dev_service.exec_turn`` — DB-free) and push stream
events to a thread-safe queue; the MAIN thread drains the queue and emits AgentEvents
(the only DB writer). Comments in English to match the Code/core convention.
"""
import logging
import os
import queue
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from backend.extensions import db
from backend.models.agent import (
    AgentEventLevel,
    AgentEventType,
    AgentRun,
    AgentRunStatus,
)
from backend.models.code import CodeProject
from backend.models.code.fullstack import CodeDevSession, CodeDevTask, DevSessionStatus
from backend.services.agent.context_ledger import ContextLedger
from backend.services.agent.workflows import _verify_support
from backend.services.agent.workflows.code_dev_turn_workflow import (
    _build_turn_prompt,
    _emit_template_resolution,
    _is_restart_trigger,
    _load_contract_block,
    _resolve_source_for_project,
    _source_digest,
    checklist_board,
    load_dev_ledger,
    persist_source_snapshot,
    seed_checklist,
    sync_checklist,
)
from backend.services.code import dev_sprint_service, house_rules
from backend.services.code.dev_service import get_dev_service
from backend.services.code.frontend_project_service import get_frontend_project_service

logger = logging.getLogger(__name__)

_MAX_LANES = int(os.getenv("DEV_MODE_MAX_PARALLEL", "4"))
_LEDGER_RENDER_CHARS = int(os.getenv("DEV_MODE_LEDGER_CHARS", "4000"))
_LANE_NOTE = (
    "\n\n# 并行分片说明\n本回合是多 subagent 并行开发的一个分片:只编辑与本诉求相关的模块源码,"
    "尽量不改动共享入口(App/路由/全局 store)以免与其它分片冲突;不要运行 npm 安装/构建"
    "(集成完成后由平台统一验证与热更)。"
)


def _emit_lane_event(recorder, step, lane: int, event: dict, flags: dict | None = None) -> None:
    """Translate one lane's stream-json event into a lane-tagged AgentEvent (main thread).

    Sets ``flags['config']`` when a lane touches package.json / vite.config (needs a
    dev-server restart after integration)."""
    etype = event.get("type")
    tag = f"[分片{lane + 1}] "
    if etype == "assistant":
        for block in event.get("message", {}).get("content", []):
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name") or ""
            inp = block.get("input") or {}
            if name in ("Write", "Edit"):
                f = inp.get("file_path") or inp.get("path") or ""
                if flags is not None and _is_restart_trigger(f):
                    flags["config"] = True
                recorder.emit(
                    AgentEventType.FILE_CREATED, step_id=step.id,
                    message=f"{tag}写入 {f}", payload={"lane": lane, "tool": name, "file": f},
                )
            else:
                cmd = inp.get("command") or ""
                recorder.emit(
                    AgentEventType.TOOL_CALL, step_id=step.id,
                    message=f"{tag}{name}: {cmd[:70]}" if cmd else f"{tag}{name}",
                    payload={"lane": lane, "tool": name, "command": cmd[:400]},
                )


def _lane_prompt(project, injected, contract_block, instruction, features):
    return _build_turn_prompt(project, injected, contract_block, instruction, features, False) + _LANE_NOTE


def run_code_dev_parallel_turn_workflow(ctx, recorder) -> dict:
    """Entry point for ``code_dev_parallel_turn`` — parallel multi-feature dev turn."""
    dev = get_dev_service()
    service = get_frontend_project_service()
    cfg = ctx.config or {}
    session_id = cfg.get("session_id")
    # Two callers: the sprint scheduler (P3) passes `tasks` (each carries a task_id +
    # brief) → task mode, where the verify tail advances EACH task's state machine on
    # its OWN acceptance criteria. A manual multi-lane turn passes `lanes` (free-form
    # instructions) → whole-app checklist reconciliation. Task mode wins when present.
    raw_tasks = cfg.get("tasks") or []
    raw_lanes = cfg.get("lanes") or []
    if raw_tasks:
        task_mode = True
        lanes = [
            {"instruction": (t.get("instruction") or "").strip(),
             "feature_ids": t.get("feature_ids") or [], "task_id": t.get("task_id")}
            for t in raw_tasks
            if isinstance(t, dict) and (t.get("instruction") or "").strip() and t.get("task_id")
        ][:_MAX_LANES]
    else:
        task_mode = False
        lanes = [
            {"instruction": (ln.get("instruction") or "").strip(),
             "feature_ids": ln.get("feature_ids") or [], "task_id": None}
            for ln in raw_lanes
            if isinstance(ln, dict) and (ln.get("instruction") or "").strip()
        ][:_MAX_LANES]

    total_steps = 3
    completed = 0
    batch_tasks: list[CodeDevTask] = []

    def progress(current: str) -> None:
        run = db.session.get(AgentRun, ctx.run_id)
        if run:
            run.set_progress({
                "total_steps": total_steps, "completed_steps": completed,
                "failed_steps": 0, "current_step": current,
            })
            db.session.commit()

    def cancel_result(pid) -> dict:
        recorder.emit(AgentEventType.WARNING, level=AgentEventLevel.WARNING,
                      message="收到取消请求，已停止并行开发")
        return {"status": AgentRunStatus.CANCELLED, "resource_id": pid}

    ledger = ContextLedger.empty()
    injected = ""
    contract_block = ""
    features: list[dict] = []

    # --- Step 1: prepare -----------------------------------------------------
    with recorder.step(
        "dev_prepare", "并行开发准备", "planner", 1,
        input_summary=f"{len(lanes)} 个并行分片",
    ) as step:
        if not ctx.resource_id:
            raise ValueError("缺少 resource_id：开发模式需要一个已有的 Code 项目")
        project = CodeProject.query.filter_by(id=ctx.resource_id, user_id=ctx.user_id).first()
        if not project:
            raise ValueError("项目不存在或无权访问")
        session = db.session.get(CodeDevSession, session_id) if session_id else None
        if not session or session.project_id != project.id or session.user_id != ctx.user_id:
            raise ValueError("开发会话不存在或无权访问")
        if not lanes:
            raise ValueError("并行开发需要至少一个有效分片")
        project_id = project.id
        if not dev.is_available():
            raise RuntimeError("开发模式不可用：未配置容器运行时或 Anthropic 凭证")

        ledger = load_dev_ledger(session, project)
        for ln in lanes:
            ledger.record_user_revision("dev", ln["instruction"])
        run = db.session.get(AgentRun, ctx.run_id)
        run.set_context_ledger(ledger.to_dict())
        session.set_shared_ledger(ledger.to_dict())
        db.session.commit()
        seed_checklist(session.id, project_id, ledger.to_dict())
        if task_mode:
            # Bind each claimed task to THIS run (queued -> in_progress) so a crash
            # mid-run is reconcilable by last_attempt_run_id, then build the verify set
            # from the UNION of every task's OWN acceptance criteria (not the ledger —
            # mirrors the single-turn task path).
            for ln in lanes:
                dev_sprint_service.mark_in_progress(ln["task_id"], ctx.run_id)
            db.session.expire_all()
            batch_tasks = [
                t for t in (db.session.get(CodeDevTask, ln["task_id"]) for ln in lanes)
                if t is not None
            ]
            features = []
            for t in batch_tasks:
                features.extend(dev_sprint_service.ac_feature_items(t))
        else:
            features = _verify_support.features_from_ledger(ledger.to_dict())

        status = dev.container_status(project_id)
        if not status.get("running"):
            session.status = DevSessionStatus.STARTING
            db.session.commit()
            recorder.emit(AgentEventType.PROGRESS, step_id=step.id,
                          message="正在启动长运行开发容器…")
            contract_block = _load_contract_block(project_id)
            source_resolution = _resolve_source_for_project(project, contract_block)
            _emit_template_resolution(recorder, step.id, source_resolution)
            ok, err, info = dev.start_container(project_id, source_resolution.files)
            if not ok:
                session.status = DevSessionStatus.FAILED
                session.error_message = err
                db.session.commit()
                raise RuntimeError(f"开发容器启动失败：{err}")
            session.container_name = info.get("container_name")
            session.internal_port = info.get("internal_port")
            session.workdir = info.get("workdir")
            session.preview_path = info.get("preview_path")
            db.session.commit()
        session.status = DevSessionStatus.RUNNING
        session.last_active_at = datetime.utcnow()
        db.session.commit()
        recorder.emit(AgentEventType.DEV_PREVIEW_READY, step_id=step.id,
                      message="开发容器就绪，实时预览可用",
                      payload={"url": session.preview_path, "container": session.container_name})
        injected = ledger.render_for_prompt(max_chars=_LEDGER_RENDER_CHARS)
        try:
            from backend.services.code.fullstack import contract_service
            _row = contract_service.get_ledger(project_id)
            if _row and _row.contract_status == "ready":
                contract_block = contract_service.render_contract_for_prompt(
                    _row.get_api_contract(), include_db_schema=False)
        except Exception:  # noqa: BLE001
            contract_block = ""
        step.set_output(output_summary=f"准备就绪:{len(lanes)} 个并行开发分片。")
    completed = 1
    progress("parallel")
    if ctx.is_cancelled():
        return cancel_result(project_id)

    # --- Step 2: parallel edit + integration barrier -------------------------
    with recorder.step(
        "dev_parallel", "并行开发 Agent", "generator", 2,
        input_summary="多分片隔离编辑 + 集成合并",
    ) as step:
        step.model_provider = "claude-code-cli (docker exec ×N)"
        db.session.commit()
        # Track package.json / vite.config edits across all lanes → restart after.
        _cfg = {"config": False}

        # Per-lane feature block for the EDIT prompt: in task mode each lane sees ONLY
        # its own task's acceptance criteria (not the batch union) so it doesn't drift
        # into a sibling's feature; manual mode shares the ledger feature list. Built on
        # the MAIN thread (reads ORM) before the fan-out threads (DB-free) start.
        _task_by_id = {t.id: t for t in batch_tasks}

        def _lane_feats(lane):
            if task_mode:
                t = _task_by_id.get(lane.get("task_id"))
                return dev_sprint_service.ac_feature_items(t) if t is not None else []
            return features

        use_git = dev.git_ready(project_id)
        worktrees: dict[int, str] = {}
        if use_git:
            for i in range(len(lanes)):
                wt = dev.create_worktree(project_id, i)
                if wt:
                    worktrees[i] = wt
            if len(worktrees) < len(lanes):
                # Some worktrees failed → safest is a fully serial pass.
                use_git = False
                dev.cleanup_worktrees(project_id, list(range(len(lanes))))
                worktrees = {}

        results: dict[int, object] = {}
        if use_git:
            recorder.emit(AgentEventType.PROGRESS, step_id=step.id,
                          message=f"并行开发 {len(lanes)} 个分片(各自独立 worktree 隔离)",
                          payload={"lanes": len(lanes), "mode": "parallel"})
            ev_q: "queue.Queue" = queue.Queue()

            # Build every lane prompt on the MAIN thread — _build_turn_prompt reads
            # lazy ORM attributes on `project`, which would raise "outside application
            # context" if touched inside a fan-out worker thread. The threads then use
            # the prebuilt strings and only do DB-free subprocess work.
            lane_prompts = [
                _lane_prompt(project, injected, contract_block, ln["instruction"], _lane_feats(ln))
                for ln in lanes
            ]

            def _thunk(idx):
                res = dev.exec_turn(
                    project_id, lane_prompts[idx],
                    on_event=lambda e, _i=idx: ev_q.put((_i, e)),
                    is_cancelled=ctx.is_cancelled, workdir=worktrees[idx],
                )
                return idx, res

            with ThreadPoolExecutor(max_workers=min(len(lanes), 8)) as ex:
                futures = [ex.submit(_thunk, i) for i in range(len(lanes))]
                while not all(f.done() for f in futures):
                    try:
                        i, event = ev_q.get(timeout=0.3)
                        _emit_lane_event(recorder, step, i, event, _cfg)
                    except queue.Empty:
                        pass
                while not ev_q.empty():
                    i, event = ev_q.get_nowait()
                    _emit_lane_event(recorder, step, i, event, _cfg)
                for f in futures:
                    try:
                        idx, res = f.result()
                        results[idx] = res
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("parallel lane raised: %s", exc)

            if ctx.is_cancelled():
                dev.cleanup_worktrees(project_id, list(range(len(lanes))))
                return cancel_result(project_id)

            # Integration barrier: merge clean lanes; re-apply conflicting/failed serially.
            to_serial: list[int] = []
            for i in range(len(lanes)):
                res = results.get(i)
                if res is not None and getattr(res, "success", False):
                    dev.commit_worktree(project_id, i)
                    ok, conflicts = dev.merge_lane(project_id, i)
                    if ok:
                        recorder.emit(AgentEventType.PROGRESS, step_id=step.id,
                                      message=f"分片{i + 1} 已集成合并", payload={"lane": i, "merged": True})
                    else:
                        to_serial.append(i)
                        recorder.emit(AgentEventType.WARNING, level=AgentEventLevel.WARNING, step_id=step.id,
                                      message=f"分片{i + 1} 合并冲突({len(conflicts)} 文件)，改为串行重做",
                                      payload={"lane": i, "conflicts": conflicts[:20]})
                else:
                    to_serial.append(i)
                    recorder.emit(AgentEventType.WARNING, level=AgentEventLevel.WARNING, step_id=step.id,
                                  message=f"分片{i + 1} 未干净完成，改为串行重做", payload={"lane": i})
            dev.cleanup_worktrees(project_id, list(range(len(lanes))))
            serial_lanes = [lanes[i] for i in to_serial]
        else:
            recorder.emit(AgentEventType.WARNING, level=AgentEventLevel.WARNING, step_id=step.id,
                          message="容器内无 git 隔离能力,本回合改为串行逐个开发(仍会全部完成)",
                          payload={"mode": "serial"})
            serial_lanes = lanes

        # Serial pass (git-unavailable, or conflicting/failed lanes) — on /work directly.
        for si, lane in enumerate(serial_lanes):
            if ctx.is_cancelled():
                return cancel_result(project_id)
            recorder.emit(AgentEventType.PROGRESS, step_id=step.id,
                          message=f"串行开发:{lane['instruction'][:60]}",
                          payload={"serial": si + 1, "of": len(serial_lanes)})
            prompt = _build_turn_prompt(project, injected, contract_block, lane["instruction"], _lane_feats(lane), False)
            r = dev.exec_turn(
                project_id, prompt,
                on_event=lambda e: _emit_lane_event(recorder, step, 0, e, _cfg),
                is_cancelled=ctx.is_cancelled,
            )
            if getattr(r, "cancelled", False):
                return cancel_result(project_id)

        # A change to package.json / vite.config in any lane needs a dev-server restart
        # so Vite reloads config + installs new deps (a stale plugin would 500 otherwise).
        if _cfg["config"] and not ctx.is_cancelled():
            recorder.emit(AgentEventType.PROGRESS, step_id=step.id,
                          message="检测到依赖/构建配置变更,正在重启开发服务器…", payload={"restart": True})
            if dev.restart_dev_server(project_id):
                recorder.emit(AgentEventType.DEV_PREVIEW_READY, step_id=step.id,
                              message="开发服务器已重启,预览已刷新", payload={"restarted": True})
        step.set_output(output_summary=f"并行开发完成:{len(lanes)} 个分片已集成。")
    completed = 2
    progress("verify")
    if ctx.is_cancelled():
        return cancel_result(project_id)

    # --- Step 3: verify + checklist ------------------------------------------
    with recorder.step(
        "dev_verify", "集成验证与验收", "reviewer", 3,
        input_summary="房规 + 验收评审 + 更新功能清单",
    ) as step:
        files = dev.collect_source(project_id)
        violations = house_rules.check_frontend(files) if files else []
        review = None
        if files:
            try:
                review = service.review_project(
                    source_digest=_source_digest(files),
                    requirements_registry=injected,
                    style_prompt=project.style_prompt or "",
                    features_block=_verify_support.render_features_block(features),
                    house_rules_report=house_rules.render_report(violations),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("parallel verify review raised: %s", exc)
                review = None
        feats, stats = _verify_support.apply_feature_results(features, (review or {}).get("feature_results"))
        # Post-merge runtime smoke: load + drive the MERGED app in a real browser so a
        # semantically-broken-but-textually-clean merge (git only catches text conflicts)
        # is caught as an OBJECTIVE blocker instead of being marked DONE. Fail-open — a
        # smoke that can't run ({"ran": False}) contributes no errors, never blocks.
        runtime_check = None
        if files and not ctx.is_cancelled():
            try:
                runtime_check = dev.runtime_smoke(project_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("parallel runtime smoke raised: %s", exc)
                runtime_check = None
            _rt_errs = _verify_support.runtime_errors(runtime_check)
            if runtime_check and runtime_check.get("ran"):
                if _rt_errs:
                    recorder.emit(
                        AgentEventType.WARNING, level=AgentEventLevel.WARNING, step_id=step.id,
                        message=f"运行时冒烟发现 {len(_rt_errs)} 处错误(合并后浏览器实际抛出)",
                        payload={"runtime_errors": _rt_errs[:10]},
                    )
                else:
                    recorder.emit(
                        AgentEventType.PROGRESS, step_id=step.id,
                        message="运行时冒烟通过:合并后应用可加载并驱动,无 console/page error",
                    )
        verification = _verify_support.Verification(
            house_rule_errors=house_rules.errors(violations),
            house_rule_warnings=house_rules.warnings(violations),
            runtime_errors=_verify_support.runtime_errors(runtime_check),
            runtime_check=runtime_check,
            review=review, features=feats, feature_stats=stats,
        )
        # Durable snapshot so a re-entry restores the work (container fs dies on stop).
        persist_source_snapshot(step, project_id, files)
        task_outcomes: list[dict] = []
        if task_mode:
            # Drive EACH task's state machine on its OWN acceptance criteria (sibling AC
            # masked out of each task's regression view; objective_blocking is batch-level
            # — a broken merged tree fails everyone). See apply_batch_verify_outcomes.
            task_outcomes = dev_sprint_service.apply_batch_verify_outcomes(
                batch_tasks, ctx.run_id, feats, verification.objective_blocking,
                verification.summary_line(),
            )
            for t, outcome in zip(batch_tasks, task_outcomes):
                _fid = t.feature_id or t.id[:8]
                if outcome["passed"]:
                    msg = f"任务 [{_fid}] 验收通过"
                elif outcome.get("shared_block"):
                    msg = f"任务 [{_fid}] 自身验收通过,但合并树存在共享阻断,已无损重排(不计重试)"
                else:
                    msg = f"任务 [{_fid}] 未通过（→ {outcome['status']}）"
                recorder.emit(
                    AgentEventType.CHECKLIST_UPDATED, step_id=step.id,
                    message=msg, payload={"task_id": t.id, "task_outcome": outcome},
                )
            board = checklist_board(session.id)
            recorder.emit(
                AgentEventType.CHECKLIST_UPDATED, step_id=step.id,
                message=f"功能进度 {board['functional_done']}/{board['functional_total']}",
                payload={"board": board, "feature_stats": stats, "task_outcomes": task_outcomes},
            )
        else:
            board = sync_checklist(session.id, project_id, feats, ctx.run_id)
            recorder.emit(AgentEventType.CHECKLIST_UPDATED, step_id=step.id,
                          message=f"功能进度 {board['functional_done']}/{board['functional_total']}",
                          payload={"board": board, "feature_stats": stats})
        session.set_shared_ledger(ledger.to_dict())
        session.last_active_at = datetime.utcnow()
        db.session.commit()
        step.add_artifact("json", "并行回合验证结果", content_json=verification.to_record(),
                          domain_ref_type="code_dev_turn", domain_ref_id=session.id)
        step.set_output(output_summary=f"验证:{verification.summary_line()}")
    completed = 3
    progress("done")
    result = {"status": AgentRunStatus.COMPLETED, "resource_id": project_id}
    if task_mode:
        result["task_outcomes"] = task_outcomes
    return result
