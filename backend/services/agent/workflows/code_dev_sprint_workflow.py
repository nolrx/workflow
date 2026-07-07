"""
Dev Mode sprint workflow (``code_dev_sprint``) — the serial task scheduler.

Turns the one-shot "generate a project zip" model into a persistent multi-turn
developer: the backlog lives in the DB (``CodeDevTask`` state machine), the code
lives in the long-running dev container, and this orchestrating run just loops:

    claim ONE ready task (atomic pending->queued)
      -> start a ``code_dev_turn`` child run carrying ONLY that task's brief
      -> drive it SYNCHRONOUSLY on this worker thread (``agent_runtime.run_sync``)
      -> the turn verifies the task's acceptance criteria and advances the task
         (done / back-to-pending / blocked)
      -> update the sprint pulse (turn budget, stall guard), next round.

Statelessness is the design invariant: every loop iteration re-reads the sprint +
tasks from the DB, so a pause (run PAUSED -> re-dispatch) or a service restart
(RESUME_FROM_SCRATCH) simply re-enters the loop and continues. The child run is a
completely ordinary ``code_dev_turn`` — its own timeline / SSE / billing / cancel
work unchanged, and the dev page's reattach logic picks it up automatically.

Threading note: ``run_sync`` executes the child's ``_execute`` on THIS thread,
whose ``finally`` does ``db.session.remove()`` — so this workflow NEVER holds ORM
instances or an open recorder step across a child run (plain-string ids only, and
rounds are narrated with events, not step contexts). Comments in English to match
the Code/core convention.
"""
import logging
import os
import threading
import time
from datetime import datetime

from flask import current_app

from backend.extensions import db
from backend.models.agent import (
    AgentEventLevel,
    AgentEventType,
    AgentRun,
    AgentRunStatus,
)
from backend.models.code import CodeProject
from backend.models.code.fullstack import (
    CodeDevSession,
    CodeDevSprint,
    CodeDevTask,
    DevSessionStatus,
    DevSprintStatus,
    DevTaskStatus,
)
from backend.services import pricing
from backend.services.agent.workflows.code_dev_turn_workflow import checklist_board
from backend.services.code import dev_backlog_planner_service, dev_sprint_service
from backend.services.credit_service import InsufficientCreditsError, deduct_credits

logger = logging.getLogger(__name__)

# Consecutive child runs ending FAILED (infra-level, not acceptance fails) that
# flip the whole sprint to failed — a systemic outage must not burn the backlog
# one task at a time.
_MAX_RUN_FAILURES = int(os.getenv("CODE_DEV_SPRINT_MAX_RUN_FAILURES", "2"))
# Patience (rounds of 5s) when tasks are ACTIVE under a run this sprint doesn't
# own (e.g. a manual turn in flight) before giving up as blocked.
_MAX_WAIT_ROUNDS = int(os.getenv("CODE_DEV_SPRINT_MAX_WAIT_ROUNDS", "60"))


def _create_turn_run(ctx, session_id: str, sprint_id: str, task: CodeDevTask) -> AgentRun | None:
    """Create (and charge) one child turn run for a claimed task — NOT dispatched
    to the executor; the sprint drives it synchronously. ``None`` = insufficient
    credits (mirrors dev_routes._start_dev_run)."""
    cost = pricing.CODE_DEV_TURN
    config = {
        "session_id": session_id,
        "sprint_id": sprint_id,
        "task_id": task.id,
        "instruction": dev_sprint_service.build_task_brief(task, _done_titles(session_id)),
        "title": f"[Sprint] {task.title[:70]}",
    }
    run = AgentRun(
        user_id=ctx.user_id,
        team_id=ctx.team_id,
        domain="code",
        workflow="code_dev_turn",
        resource_type="code_project",
        resource_id=ctx.resource_id,
        title=config["title"],
        status=AgentRunStatus.QUEUED,
        credit_reserved=cost,
    )
    run.set_config(config)
    run.set_input_snapshot({
        "domain": "code", "workflow": "code_dev_turn",
        "resource_type": "code_project", "resource_id": ctx.resource_id,
        "config": {k: v for k, v in config.items() if k != "instruction"},
    })
    db.session.add(run)
    db.session.commit()
    if cost > 0:
        try:
            deduct_credits(
                user_id=ctx.user_id, amount=cost, operation="agent_run",
                resource_type="agent_run", resource_id=run.id,
                description="Agent run: code_dev_turn (sprint)", team_id=ctx.team_id,
            )
        except InsufficientCreditsError:
            db.session.delete(run)
            db.session.commit()
            return None
    # Stamp the attempt on the task while it is still queued, so a crash between
    # here and the turn's own mark_in_progress is reconcilable by run id.
    db.session.query(CodeDevTask).filter(CodeDevTask.id == task.id).update(
        {CodeDevTask.last_attempt_run_id: run.id, CodeDevTask.updated_at: datetime.utcnow()},
        synchronize_session=False,
    )
    db.session.commit()
    return run


def _done_titles(session_id: str) -> dict:
    return {
        t.feature_id: t.title
        for t in dev_sprint_service.session_tasks(session_id)
        if t.feature_id and t.status == DevTaskStatus.DONE
    }


def _create_batch_turn_run(
    ctx, session_id: str, sprint_id: str, tasks: list[CodeDevTask]
) -> AgentRun | None:
    """Create (and charge) ONE ``code_dev_parallel_turn`` run driving a BATCH of
    claimed tasks — the P3 analogue of ``_create_turn_run``. Reserves
    ``CODE_DEV_TURN × len(tasks)`` (each task is one claude edit lane → same cost as
    running them serially). Stamps ``last_attempt_run_id`` on EVERY task so a crash
    is reconcilable by run id. ``None`` = insufficient credits."""
    done = _done_titles(session_id)
    task_briefs = [
        {
            "task_id": t.id,
            "instruction": dev_sprint_service.build_task_brief(t, done),
            "feature_ids": [t.feature_id] if t.feature_id else [],
            "ac_ids": sorted(dev_sprint_service.ac_ids_for(t)),
        }
        for t in tasks
    ]
    cost = pricing.CODE_DEV_TURN * len(tasks)
    config = {
        "session_id": session_id,
        "sprint_id": sprint_id,
        "tasks": task_briefs,
        "title": f"[Sprint 并行] {len(tasks)} 个任务",
    }
    run = AgentRun(
        user_id=ctx.user_id,
        team_id=ctx.team_id,
        domain="code",
        workflow="code_dev_parallel_turn",
        resource_type="code_project",
        resource_id=ctx.resource_id,
        title=config["title"],
        status=AgentRunStatus.QUEUED,
        credit_reserved=cost,
    )
    run.set_config(config)
    run.set_input_snapshot({
        "domain": "code", "workflow": "code_dev_parallel_turn",
        "resource_type": "code_project", "resource_id": ctx.resource_id,
        "config": {"session_id": session_id, "sprint_id": sprint_id,
                   "task_ids": [t.id for t in tasks]},
    })
    db.session.add(run)
    db.session.commit()
    if cost > 0:
        try:
            deduct_credits(
                user_id=ctx.user_id, amount=cost, operation="agent_run",
                resource_type="agent_run", resource_id=run.id,
                description=f"Agent run: code_dev_parallel_turn (sprint ×{len(tasks)})",
                team_id=ctx.team_id,
            )
        except InsufficientCreditsError:
            db.session.delete(run)
            db.session.commit()
            return None
    db.session.query(CodeDevTask).filter(
        CodeDevTask.id.in_([t.id for t in tasks])
    ).update(
        {CodeDevTask.last_attempt_run_id: run.id, CodeDevTask.updated_at: datetime.utcnow()},
        synchronize_session=False,
    )
    db.session.commit()
    return run


def run_code_dev_sprint_workflow(ctx, recorder) -> dict:
    """Entry point for ``code_dev_sprint`` — serial sprint over the task backlog."""
    cfg = ctx.config or {}
    session_id = cfg.get("session_id")
    sprint_id = cfg.get("sprint_id")
    project_id = ctx.resource_id
    app = current_app._get_current_object()

    def _sprint() -> CodeDevSprint | None:
        return db.session.get(CodeDevSprint, sprint_id) if sprint_id else None

    def _emit_pulse(message: str, level=AgentEventLevel.INFO, extra: dict | None = None) -> None:
        sprint = _sprint()
        payload = {"sprint": sprint.to_dict() if sprint else None,
                   "board": checklist_board(session_id)}
        payload.update(extra or {})
        recorder.emit(
            AgentEventType.CHECKLIST_UPDATED, level=level, message=message, payload=payload,
        )

    def _finish(status: str, reason: str, run_status: str = AgentRunStatus.COMPLETED) -> dict:
        """Settle the sprint row terminally + publish the summary artifact."""
        sprint = _sprint()
        if sprint and sprint.status not in DevSprintStatus.TERMINAL:
            sprint.status = status
            sprint.finished_at = datetime.utcnow()
            snap = dev_sprint_service.progress_snapshot(session_id, sprint.lane)
            snap["reason"] = reason
            sprint.set_progress_snapshot(snap)
            sprint.set_current_task_ids([])
            db.session.commit()
        level = AgentEventLevel.INFO if status == DevSprintStatus.COMPLETED else AgentEventLevel.WARNING
        _emit_pulse(f"Sprint {status}：{reason}", level=level)
        if run_status == AgentRunStatus.COMPLETED:
            with recorder.step(
                "sprint_publish", "Sprint 交付", "publisher", 2,
                input_summary="汇总任务板与执行结果",
            ) as step:
                sprint = _sprint()
                step.add_artifact(
                    "json", "Sprint 执行摘要",
                    content_json={
                        "sprint": sprint.to_dict() if sprint else None,
                        "board": checklist_board(session_id),
                        "reason": reason,
                    },
                    domain_ref_type="code_dev_sprint", domain_ref_id=sprint_id,
                )
                step.set_output(output_summary=f"Sprint {status}：{reason}")
        return {"status": run_status, "resource_id": project_id}

    def _finalize_cancelled() -> dict:
        sprint = _sprint()
        if sprint:
            for tid in sprint.get_current_task_ids():
                dev_sprint_service.mark_cancelled(tid)
            if sprint.status not in DevSprintStatus.TERMINAL:
                sprint.status = DevSprintStatus.CANCELLED
                sprint.finished_at = datetime.utcnow()
                sprint.set_current_task_ids([])
                db.session.commit()
        recorder.emit(
            AgentEventType.WARNING, level=AgentEventLevel.WARNING,
            message="收到取消请求，Sprint 已停止",
        )
        return {"status": AgentRunStatus.CANCELLED, "resource_id": project_id}

    # --- Step 1: plan (bounded; completes BEFORE any child run) ---------------
    with recorder.step(
        "sprint_plan", "Sprint 规划", "planner", 1,
        input_summary="加载任务板 + 状态对账",
    ) as step:
        if not project_id:
            raise ValueError("缺少 resource_id：Sprint 需要一个已有的 Code 项目")
        project = CodeProject.query.filter_by(id=project_id, user_id=ctx.user_id).first()
        if not project:
            raise ValueError("项目不存在或无权访问")
        session = db.session.get(CodeDevSession, session_id) if session_id else None
        if not session or session.project_id != project_id or session.user_id != ctx.user_id:
            raise ValueError("开发会话不存在或无权访问")
        sprint = _sprint()
        if not sprint or sprint.session_id != session.id:
            raise ValueError("Sprint 不存在或不属于本开发会话")
        cancelled_early = sprint.status == DevSprintStatus.CANCELLED
        if cancelled_early:
            step.set_output(output_summary="Sprint 已被取消，无需调度。")
        if sprint.status in DevSprintStatus.TERMINAL and not cancelled_early:
            raise ValueError(f"Sprint 已结束（{sprint.status}）")
        if session.lane != "frontend":
            raise ValueError("P0 阶段 Sprint 仅支持前端开发会话")
        # Rebind on a fresh orchestrator run (resume-after-restart path).
        if sprint.run_id != ctx.run_id:
            sprint.run_id = ctx.run_id
        healed = dev_sprint_service.reconcile_stale_tasks(session.id)
        dead = dev_sprint_service.block_dead_dependency_tasks(session.id)
        # Auto-split coarse ledger-seed tasks (one whole FR, no granular AC) into
        # small winnable sub-tasks BEFORE scheduling — a monolithic FR can't be
        # delivered AND pass the adversarial reviewer in a single turn, so it would
        # otherwise verify->repair->retry->blocked every time. Advisory: any failure
        # leaves the board exactly as it was (the coarse task simply runs as before).
        decomposed = {"candidates": 0, "decomposed": 0, "sub_tasks": 0, "unsplit": 0}
        if not cancelled_early:
            try:
                decomposed = dev_backlog_planner_service.decompose_coarse_seed_tasks(
                    project, session, style_hint=project.style_prompt or "",
                )
            except Exception:  # noqa: BLE001 — a split failure must not sink the sprint
                logger.warning("sprint auto-decompose raised", exc_info=True)
            if decomposed["decomposed"]:
                recorder.emit(
                    AgentEventType.PROGRESS, step_id=step.id,
                    message=(
                        f"已将 {decomposed['decomposed']} 个粗需求细拆为 "
                        f"{decomposed['sub_tasks']} 个可单回合完成的子任务(带具体验收标准)。"
                    ),
                    payload=decomposed,
                )
        # Don't clobber a pause/cancel that raced in between create and dispatch —
        # the loop's first iteration honours PAUSING/CANCELLED immediately.
        if sprint.status in (DevSprintStatus.PLANNED, DevSprintStatus.PAUSED):
            sprint.status = DevSprintStatus.RUNNING
        sprint.set_current_task_ids([])
        snap = dev_sprint_service.progress_snapshot(session.id, session.lane)
        sprint.set_progress_snapshot(snap)
        db.session.commit()
        max_turns = sprint.max_turns or dev_sprint_service.sprint_default_max_turns()
        stall_limit = dev_sprint_service.sprint_stall_limit()
        if not cancelled_early:
            recon = (
                f"对账:恢复中断任务 {len(healed)} 项,死依赖阻塞 {len(dead)} 项。"
                if (healed or dead) else "任务板状态一致,无需对账修复。"
            )
            if decomposed["decomposed"] or decomposed["unsplit"]:
                recon += (
                    f" 起跑前细拆:{decomposed['decomposed']} 个粗需求→"
                    f"{decomposed['sub_tasks']} 个子任务"
                    + (f",{decomposed['unsplit']} 个暂未拆分(按原样执行)" if decomposed["unsplit"] else "")
                    + "。"
                )
            step.set_output(
                output_summary=(
                    f"任务板 {snap['total']} 项（就绪 {snap['ready']}，已完成 {snap['done']}）；"
                    f"回合预算 {max_turns}，停滞阈值 {stall_limit}。"
                ),
                self_check=recon,
            )
    if cancelled_early:
        return _finalize_cancelled()

    # --- Scheduling loop (events only — NO step context across child runs) ----
    consecutive_failures = 0
    wait_rounds = 0
    while True:
        db.session.expire_all()
        sprint = _sprint()
        session = db.session.get(CodeDevSession, session_id)

        if ctx.is_cancelled() or (sprint and sprint.status == DevSprintStatus.CANCELLED):
            return _finalize_cancelled()
        if sprint.status == DevSprintStatus.PAUSING:
            sprint.status = DevSprintStatus.PAUSED
            db.session.commit()
            _emit_pulse("Sprint 已暂停（当前回合已收尾，可随时恢复）")
            return {"status": AgentRunStatus.PAUSED, "resource_id": project_id}
        if session.status in DevSessionStatus.TERMINAL:
            return _finish(DevSprintStatus.BLOCKED, "开发会话已停止,请重新启动开发模式后恢复")
        if sprint.turn_count >= max_turns:
            return _finish(DevSprintStatus.BLOCKED, f"回合预算用尽（{sprint.turn_count}/{max_turns}）")
        if sprint.stall_count >= stall_limit:
            return _finish(
                DevSprintStatus.BLOCKED, f"连续 {sprint.stall_count} 回合无新完成任务,已停止"
            )

        dev_sprint_service.block_dead_dependency_tasks(session_id)
        # P3 (env-gated, default OFF): claim a batch of independent ready tasks and drive
        # them through one parallel turn. OFF → the single-task serial path below is
        # byte-for-byte unchanged.
        parallel = dev_sprint_service.sprint_parallel_enabled()
        if parallel:
            batch = dev_sprint_service.claim_ready_batch(
                session_id, session.lane, dev_sprint_service.sprint_batch_size()
            )
        else:
            _one = dev_sprint_service.claim_next_task(session_id, session.lane)
            batch = [_one] if _one else []
        if not batch:
            # Nothing claimable: heal interrupted claims first, then judge the board.
            if dev_sprint_service.reconcile_stale_tasks(session_id):
                continue
            snap = dev_sprint_service.progress_snapshot(session_id, session.lane)
            active = snap["counts"].get(DevTaskStatus.QUEUED, 0) \
                + snap["counts"].get(DevTaskStatus.IN_PROGRESS, 0) \
                + snap["counts"].get(DevTaskStatus.VERIFYING, 0)
            if active:
                # A live turn this sprint doesn't own (e.g. a manual chat turn).
                wait_rounds += 1
                if wait_rounds > _MAX_WAIT_ROUNDS:
                    return _finish(DevSprintStatus.BLOCKED, "存在他方执行中的任务且长时间未结束")
                time.sleep(5)
                continue
            if snap["unsettled"] == 0 and snap["settled_ok"] == snap["total"]:
                return _finish(DevSprintStatus.COMPLETED, f"全部 {snap['total']} 项任务已交付/跳过")
            if snap["unsettled"] == 0:
                return _finish(
                    DevSprintStatus.BLOCKED,
                    "存在 blocked/failed/cancelled 任务,需人工处理后重开 Sprint",
                )
            return _finish(DevSprintStatus.BLOCKED, "无可调度任务（依赖链等待或全部被阻塞）")
        wait_rounds = 0

        # ==== parallel batch round (P3) — one code_dev_parallel_turn over the batch ===
        if parallel:
            batch_ids = [t.id for t in batch]
            batch_fids = [t.feature_id or t.id[:8] for t in batch]
            batch_run = _create_batch_turn_run(ctx, session_id, sprint_id, batch)
            if batch_run is None:
                for tid in batch_ids:
                    dev_sprint_service.release_to_pending(
                        tid, note="积分不足,任务退回队列", count_retry=False)
                return _finish(DevSprintStatus.BLOCKED, "积分不足,Sprint 已停止", AgentRunStatus.COMPLETED)
            batch_run_id = batch_run.id
            sprint.turn_count += 1
            turn_no = sprint.turn_count
            sprint.set_current_task_ids(batch_ids)
            db.session.commit()
            recorder.emit(
                AgentEventType.PROGRESS,
                message=(
                    f"回合 {turn_no}/{max_turns}（并行 {len(batch)} 任务）："
                    + "、".join(f"[{f}]" for f in batch_fids[:4])
                    + ("…" if len(batch_fids) > 4 else "")
                ),
                payload={"turn": turn_no, "task_ids": batch_ids,
                         "turn_run_id": batch_run_id, "parallel": True},
            )
            run = db.session.get(AgentRun, ctx.run_id)
            if run:
                run.set_progress({
                    "total_steps": max_turns, "completed_steps": turn_no - 1,
                    "failed_steps": 0, "current_step": f"并行 {len(batch)} 个任务",
                })
                db.session.commit()

            # Drive the batch child ON THIS THREAD with sprint-level cancel forwarding
            # (in-memory only — the thread stays DB-free while the child owns the session).
            from backend.services.agent.runtime import agent_runtime

            stop_evt = threading.Event()

            def _forward_cancel_batch(child_id: str = batch_run_id) -> None:
                while not stop_evt.wait(2.0):
                    if ctx.is_cancelled():
                        agent_runtime.request_cancel(child_id)
                        return

            watcher = threading.Thread(target=_forward_cancel_batch, daemon=True)
            watcher.start()
            try:
                agent_runtime.run_sync(app, batch_run_id)
            finally:
                stop_evt.set()

            db.session.expire_all()
            child = db.session.get(AgentRun, batch_run_id)
            sprint = _sprint()
            child_status = child.status if child else AgentRunStatus.FAILED

            if child_status == AgentRunStatus.CANCELLED:
                return _finalize_cancelled()
            if child_status == AgentRunStatus.FAILED:
                consecutive_failures += 1
                reason = (child.error_message if child else None) or "并行回合运行异常"
                # Infra-level failure of the whole batch: re-queue every still-ACTIVE task
                # WITHOUT counting a retry — a systemic outage must not burn the backlog.
                for tid in batch_ids:
                    row = db.session.get(CodeDevTask, tid)
                    if row and row.status in DevTaskStatus.ACTIVE:
                        dev_sprint_service.release_to_pending(
                            tid, note=f"并行回合基础设施失败:{reason[:120]}", count_retry=False)
                recorder.emit(
                    AgentEventType.WARNING, level=AgentEventLevel.WARNING,
                    message=f"并行回合失败（{reason[:160]}），{len(batch)} 个任务已退回队列",
                    payload={"task_ids": batch_ids, "turn_run_id": batch_run_id},
                )
                if consecutive_failures >= _MAX_RUN_FAILURES:
                    sprint.status = DevSprintStatus.FAILED
                    sprint.finished_at = datetime.utcnow()
                    snap = dev_sprint_service.progress_snapshot(session_id, sprint.lane)
                    snap["reason"] = f"连续 {consecutive_failures} 个回合基础设施级失败"
                    sprint.set_progress_snapshot(snap)
                    sprint.set_current_task_ids([])
                    db.session.commit()
                    raise RuntimeError(f"Sprint 连续 {consecutive_failures} 个回合失败,已终止")
            else:
                consecutive_failures = 0
                # The batch turn advanced each task via apply_verify_outcome; heal any task
                # left dangling ACTIVE (child crashed between tasks) as an interrupted attempt.
                for tid in batch_ids:
                    row = db.session.get(CodeDevTask, tid)
                    if row and row.status in DevTaskStatus.ACTIVE:
                        if row.effective_retry_count < dev_sprint_service.max_retries_of(row):
                            dev_sprint_service.release_to_pending(
                                tid, note="并行回合未完成验收流转,已重新排队", count_retry=True)
                        else:
                            dev_sprint_service.mark_failed(tid, "多次尝试均未完成验收流转")

            db.session.expire_all()
            sprint = _sprint()
            newly_done = any(
                (row := db.session.get(CodeDevTask, tid)) is not None
                and row.status == DevTaskStatus.DONE
                for tid in batch_ids
            )
            sprint.stall_count = 0 if newly_done else sprint.stall_count + 1
            snap = dev_sprint_service.progress_snapshot(session_id, sprint.lane)
            sprint.set_progress_snapshot(snap)
            sprint.set_current_task_ids([])
            db.session.commit()
            _emit_pulse(
                f"并行回合完成:{len(batch)} 个任务;进度 {snap['done']}/{snap['total']}",
                extra={"task_ids": batch_ids, "turn_run_id": batch_run_id},
            )
            continue

        # ---- one scheduling round (plain-string ids across the child run) ----
        task = batch[0]
        task_id = task.id
        task_fid = task.feature_id or task.id[:8]
        task_title = task.title
        turn_run = _create_turn_run(ctx, session_id, sprint_id, task)
        if turn_run is None:
            dev_sprint_service.release_to_pending(task_id, note="积分不足,任务退回队列", count_retry=False)
            return _finish(DevSprintStatus.BLOCKED, "积分不足,Sprint 已停止", AgentRunStatus.COMPLETED)
        turn_run_id = turn_run.id
        sprint.turn_count += 1
        turn_no = sprint.turn_count
        sprint.set_current_task_ids([task_id])
        db.session.commit()
        recorder.emit(
            AgentEventType.PROGRESS,
            message=f"回合 {turn_no}/{max_turns}：任务 [{task_fid}] {task_title[:60]}",
            payload={"turn": turn_no, "task_id": task_id, "turn_run_id": turn_run_id},
        )
        run = db.session.get(AgentRun, ctx.run_id)
        if run:
            run.set_progress({
                "total_steps": max_turns, "completed_steps": turn_no - 1,
                "failed_steps": 0, "current_step": f"[{task_fid}] {task_title[:40]}",
            })
            db.session.commit()

        # Forward a sprint-level cancel to the in-flight child (in-memory check
        # only — this thread must stay DB-free while the child owns the session).
        from backend.services.agent.runtime import agent_runtime

        stop_evt = threading.Event()

        def _forward_cancel(child_id: str = turn_run_id) -> None:
            while not stop_evt.wait(2.0):
                if ctx.is_cancelled():
                    agent_runtime.request_cancel(child_id)
                    return

        watcher = threading.Thread(target=_forward_cancel, daemon=True)
        watcher.start()
        try:
            # Drive the child turn ON THIS THREAD — its finally does
            # db.session.remove(), so everything ORM must be re-fetched after.
            agent_runtime.run_sync(app, turn_run_id)
        finally:
            stop_evt.set()

        db.session.expire_all()
        child = db.session.get(AgentRun, turn_run_id)
        task_row = db.session.get(CodeDevTask, task_id)
        sprint = _sprint()
        child_status = child.status if child else AgentRunStatus.FAILED

        if child_status == AgentRunStatus.CANCELLED:
            return _finalize_cancelled()
        if child_status == AgentRunStatus.FAILED:
            consecutive_failures += 1
            reason = (child.error_message if child else None) or "回合运行异常"
            dev_sprint_service.mark_failed(task_id, f"回合运行异常:{reason}")
            recorder.emit(
                AgentEventType.WARNING, level=AgentEventLevel.WARNING,
                message=f"任务 [{task_fid}] 回合失败（{reason[:160]}），任务标记 failed",
                payload={"task_id": task_id, "turn_run_id": turn_run_id},
            )
            if consecutive_failures >= _MAX_RUN_FAILURES:
                sprint.status = DevSprintStatus.FAILED
                sprint.finished_at = datetime.utcnow()
                snap = dev_sprint_service.progress_snapshot(session_id, sprint.lane)
                snap["reason"] = f"连续 {consecutive_failures} 个回合基础设施级失败"
                sprint.set_progress_snapshot(snap)
                sprint.set_current_task_ids([])
                db.session.commit()
                raise RuntimeError(f"Sprint 连续 {consecutive_failures} 个回合失败,已终止")
        else:
            consecutive_failures = 0
            # The turn should have settled the task; a claim it left dangling
            # (crashed between steps) is healed as an interrupted attempt.
            if task_row and task_row.status in DevTaskStatus.ACTIVE:
                if task_row.effective_retry_count < dev_sprint_service.max_retries_of(task_row):
                    dev_sprint_service.release_to_pending(
                        task_id, note="回合未完成验收流转,已重新排队", count_retry=True
                    )
                else:
                    dev_sprint_service.mark_failed(task_id, "多次尝试均未完成验收流转")

        db.session.expire_all()
        task_row = db.session.get(CodeDevTask, task_id)
        sprint = _sprint()
        newly_done = bool(task_row and task_row.status == DevTaskStatus.DONE)
        sprint.stall_count = 0 if newly_done else sprint.stall_count + 1
        snap = dev_sprint_service.progress_snapshot(session_id, sprint.lane)
        sprint.set_progress_snapshot(snap)
        sprint.set_current_task_ids([])
        db.session.commit()
        _emit_pulse(
            f"任务 [{task_fid}] → {task_row.status if task_row else 'unknown'}；"
            f"进度 {snap['done']}/{snap['total']}",
            extra={"task_id": task_id, "turn_run_id": turn_run_id},
        )
