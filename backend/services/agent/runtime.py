"""
Agent runtime — background execution of workflows.

A process-wide ThreadPoolExecutor (no Celery). Each run executes in a worker
thread inside ``app.app_context()``. The runtime owns the cancel registry and
the workflow registry; the actual step logic lives in ``workflows/``.
"""
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Callable

from backend.extensions import db
from backend.models.agent import (
    AgentArtifact,
    AgentEventLevel,
    AgentEventType,
    AgentRun,
    AgentRunStatus,
    AgentStep,
    AgentStepStatus,
)
from backend.services.agent.bus import event_bus
from backend.services.agent.recorder import RunRecorder
from backend.services.agent.schemas import AgentContext
from backend.services.credit_service import refund_credits

logger = logging.getLogger(__name__)

# workflow key -> callable(ctx: AgentContext, recorder: RunRecorder) -> dict
_WORKFLOWS: dict[str, Callable] = {}


def register_workflow(key: str, fn: Callable) -> None:
    _WORKFLOWS[key] = fn


def get_workflow(key: str) -> Callable | None:
    return _WORKFLOWS.get(key)


def known_workflows() -> list[str]:
    return list(_WORKFLOWS.keys())


def _run_produced_nothing(run_id: str) -> bool:
    """True if the run produced no artifacts — i.e. it failed before any useful
    output, which is the condition under which we auto-refund the reservation."""
    return AgentArtifact.query.filter_by(run_id=run_id).count() == 0


# --- Run-finished notification producer --------------------------------------
# Surfaces long, walk-away outcomes in the generic in-app notification feed (see
# notification_service — the feed is type-driven, this is just one producer).
# Curated to avoid noise: a deploy going live, or ANY failed run. Fail-soft — a
# notification must never break the run's own bookkeeping.
_DEPLOY_WORKFLOW = "code_fullstack_deploy"


def _notify_run_finished(run: AgentRun) -> None:
    """Emit a notice to the run's owner for notable terminal outcomes."""
    try:
        from backend.services import notification_service as ns

        link = f"/apps/{run.resource_id}" if run.resource_id else None
        if run.status == AgentRunStatus.COMPLETED and run.workflow == _DEPLOY_WORKFLOW:
            ns.create_notification(
                run.user_id, ns.TYPE_CODE_DEPLOY_SUCCEEDED, level=ns.LEVEL_SUCCESS,
                data={"workflow": run.workflow, "run_id": run.id,
                      "resource_id": run.resource_id, "link": link},
                ref_type="agent_run", ref_id=run.id,
            )
            db.session.commit()
        elif run.status == AgentRunStatus.FAILED:
            ns.create_notification(
                run.user_id, ns.TYPE_RUN_FAILED, level=ns.LEVEL_ERROR,
                data={"workflow": run.workflow, "run_id": run.id,
                      "resource_id": run.resource_id,
                      "link": link if run.workflow == _DEPLOY_WORKFLOW else None},
                ref_type="agent_run", ref_id=run.id,
            )
            db.session.commit()
    except Exception:  # noqa: BLE001 — best-effort: never fail the run on a notice
        logger.error("run-finished notification failed for %s", run.id, exc_info=True)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass


# --- Resume-across-restart policy --------------------------------------------
# Which workflows a restart should RESUME (continue from their persisted state)
# instead of failing. Two strategies, by how the workflow persists progress:
#   • RESUME_AS_RETRY — the workflow keeps a per-stage cursor and writes each
#     stage's output to durable storage (CodeProject docs, ledger, AgentSteps).
#     It already knows how to re-run from an interrupted stage and reuse the
#     completed ones: we drive it through the existing one-shot ``retry``
#     directive, so restart-resume reuses the exact same, tested machinery as a
#     user-initiated retry.
#   • RESUME_FROM_SCRATCH — a short linear pipeline whose intermediate state
#     lives only in memory between steps (the built files aren't persisted until
#     the publish step). The in-flight container build can't be recovered, so the
#     only correct resume is to re-run the whole run from step 1. The workflow
#     ignores the resume directive and naturally restarts; we just keep its run
#     row alive instead of failing it.
RESUME_AS_RETRY = {"code_full_generation"}
RESUME_FROM_SCRATCH = {
    "code_frontend_project_generation",
    "code_backend_project_generation",
    "code_middleware_provisioning",
    # The iteration analysis is a single cheap planning pass with no external side
    # effects — a restart safely re-runs it from the top.
    "code_app_iteration_analysis",
    # The sprint scheduler is stateless by design (DB task board = single source
    # of truth): re-entering from the top reconciles interrupted claims and
    # simply continues scheduling. Its in-flight child turn is NOT resumable and
    # is failed by this same pass; the sprint's reconcile then retries the task.
    "code_dev_sprint",
}
_RESUMABLE = RESUME_AS_RETRY | RESUME_FROM_SCRATCH
# Everything else (notably ``code_fullstack_deploy``) is NOT auto-resumed: a crash
# mid-deploy leaves external side effects half-applied (built image, started
# container, provisioned database) that a blind re-run could compound, so it is
# failed + refunded and the user re-triggers it explicitly (deploy is idempotent
# on re-trigger — it tears down any stale container and rebuilds).

# Cap auto-resumes so a crash-loop can't re-run (potentially expensive) work
# forever: after this many restart-resumes a run is failed instead. Tracked in
# the run's CONFIG (``_resume_count``) rather than progress, because the
# from-scratch workflows overwrite the whole progress blob on their first step.
MAX_RESUME_ATTEMPTS = int(os.getenv("AGENT_MAX_RESUME_ATTEMPTS", "3"))


def _resume_count(run: AgentRun) -> int:
    try:
        return int((run.get_config() or {}).get("_resume_count", 0))
    except (TypeError, ValueError):
        return 0


def _fail_running_steps(run_id: str, message: str) -> None:
    """Mark steps left RUNNING by the dead worker as failed.

    Keeps the timeline honest (an interrupted step shows as failed, not a
    perpetual spinner) and makes ``code_full_generation``'s retry stage-derivation
    deterministic — it reads the latest FAILED step to decide where to resume.
    """
    for step in AgentStep.query.filter_by(run_id=run_id, status=AgentStepStatus.RUNNING).all():
        step.status = AgentStepStatus.FAILED
        step.error_message = message
        step.completed_at = datetime.utcnow()


def _fail_orphaned_run(run: AgentRun, message: str) -> None:
    """Mark an orphaned run failed, refunding the reservation if it produced nothing."""
    run_id = run.id
    _fail_running_steps(run_id, message)
    run.status = AgentRunStatus.FAILED
    run.error_message = message
    run.completed_at = datetime.utcnow()
    if run.credit_reserved and _run_produced_nothing(run_id):
        try:
            refund_credits(
                run.user_id, run.credit_reserved, "agent_run", "agent_run", run_id,
                description=f"refund interrupted {run.workflow}", team_id=run.team_id,
            )
            run.credit_used = 0
        except Exception:  # noqa: BLE001
            logger.error("Refund failed for orphaned run %s", run_id, exc_info=True)
            run.credit_used = run.credit_reserved
    db.session.commit()
    # Emit terminal events so any open SSE / polling client settles instead of
    # spinning on a perpetually-"running" run.
    try:
        recorder = RunRecorder(run_id, event_bus)
        recorder.emit(
            AgentEventType.ERROR, level=AgentEventLevel.ERROR, message="服务重启，运行被中断",
        )
        recorder.emit(
            AgentEventType.RUN_COMPLETED, message="工作流结束",
            payload={"status": AgentRunStatus.FAILED},
        )
    except Exception:  # noqa: BLE001
        logger.error("Failed to emit terminal events for orphaned run %s", run_id, exc_info=True)


def _resume_orphaned_run(app, run: AgentRun) -> None:
    """Re-dispatch an interrupted run so it continues from its persisted state.

    For a run that never started (still QUEUED, no worker had picked it up) this
    is just a fresh dispatch. For a run that was mid-flight (RUNNING) we fail its
    interrupted step, hand cursor-aware workflows a one-shot ``retry`` directive,
    and re-submit — keeping the SAME run id so a reconnecting client's stream and
    history stay continuous.
    """
    run_id = run.id
    never_started = run.started_at is None  # QUEUED row a worker never reached

    cfg = run.get_config()
    cfg["_resume_count"] = _resume_count(run) + 1
    if not never_started and run.workflow in RESUME_AS_RETRY:
        # Resume from the interrupted stage, reusing every completed stage. The
        # workflow consumes (and clears) this one-shot directive on launch.
        cfg["_resume"] = {"action": "retry", "stage": None, "reason": "service_restart"}
    run.set_config(cfg)

    if not never_started:
        _fail_running_steps(run_id, "服务重启中断")
        run.status = AgentRunStatus.RUNNING  # keep ACTIVE; started_at stays set
    db.session.commit()

    # Narrate to any reconnecting client. A never-started run will emit its own
    # RUN_STARTED when the worker picks it up, so only resumes need a marker.
    if not never_started:
        try:
            recorder = RunRecorder(run_id, event_bus)
            recorder.emit(
                AgentEventType.PROGRESS,
                message="服务已重启，正在从中断处自动继续运行",
                payload={"resumed": True, "resume_count": cfg["_resume_count"]},
            )
        except Exception:  # noqa: BLE001
            logger.error("Failed to emit resume event for run %s", run_id, exc_info=True)

    agent_runtime.start(app, run_id)


def reconcile_orphaned_runs(app) -> int:
    """Resume — or, when unsafe, fail — runs orphaned by a dead worker on restart.

    Called once at startup. The background executor lives in-process, so when the
    process is replaced any in-flight run loses its worker and would otherwise
    hang ``running`` forever — and, counting as ACTIVE, it blocks the user from
    re-running. A freshly-started process has no in-flight runs of its own, so
    every RUNNING/QUEUED row is by definition orphaned (this deployment runs a
    single gunicorn worker, so there is no peer process still driving them).

    Resumable workflows (see ``_RESUMABLE``) are RE-DISPATCHED to continue from
    their persisted phase, so a restart no longer loses the user's running state.
    Non-resumable runs, and any run that has already exhausted its resume budget
    (crash-loop guard), are failed + refunded instead. PAUSED runs are left alone
    — they await user input, not a worker. Returns how many rows were handled.
    """
    with app.app_context():
        orphans = AgentRun.query.filter(
            AgentRun.status.in_([AgentRunStatus.RUNNING, AgentRunStatus.QUEUED])
        ).all()
        resumed = 0
        failed = 0
        for run in orphans:
            try:
                never_started = run.started_at is None
                resumable = never_started or run.workflow in _RESUMABLE
                if resumable and _resume_count(run) < MAX_RESUME_ATTEMPTS:
                    _resume_orphaned_run(app, run)
                    resumed += 1
                else:
                    reason = (
                        "服务多次重启，已停止自动续跑，请重新发起。"
                        if resumable
                        else "服务重启导致运行中断，请重新发起。"
                    )
                    _fail_orphaned_run(run, reason)
                    failed += 1
            except Exception:  # noqa: BLE001 — one bad row must not abort reconciliation
                logger.error("Failed to reconcile orphaned run %s", run.id, exc_info=True)
                db.session.rollback()
        if resumed or failed:
            logger.info(
                "Reconciled orphaned agent runs on startup: %d resumed, %d failed",
                resumed, failed,
            )
        return resumed + failed


class AgentRuntime:
    """Submits and supervises background agent runs."""

    def __init__(self, max_workers: int = 4):
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()

    # --- cancellation --------------------------------------------------------
    def request_cancel(self, run_id: str) -> None:
        with self._lock:
            self._cancelled.add(run_id)

    def is_cancelled(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._cancelled

    def _clear_cancel(self, run_id: str) -> None:
        with self._lock:
            self._cancelled.discard(run_id)

    # --- execution -----------------------------------------------------------
    def start(self, app, run_id: str) -> None:
        """Submit a queued run for background execution."""
        self.executor.submit(self._execute, app, run_id)

    def run_sync(self, app, run_id: str) -> None:
        """Execute a run synchronously ON THE CALLER'S THREAD.

        Used by orchestrating workflows (the dev sprint scheduler) to drive child
        turn runs serially without occupying a second executor slot or polling.
        The child gets the full ``_execute`` machinery (recorder, SSE, billing,
        refund, autosync). Caveat for callers: the child's ``finally`` does
        ``db.session.remove()``, so every ORM instance the caller held becomes
        detached — re-fetch after this returns, and never call this inside an
        open ``recorder.step(...)`` context.
        """
        self._execute(app, run_id)

    def _execute(self, app, run_id: str) -> None:
        with app.app_context():
            recorder = RunRecorder(run_id, event_bus)
            try:
                run = db.session.get(AgentRun, run_id)
                if not run:
                    logger.error("AgentRun %s not found", run_id)
                    return

                # First launch vs. resume (a paused run restarting). Only stamp
                # started_at + emit RUN_STARTED on the first launch; on resume the
                # workflow itself emits the user_revision / step events, and a
                # second RUN_STARTED would confuse the client timeline.
                first_start = run.started_at is None
                run.status = AgentRunStatus.RUNNING
                if first_start:
                    run.started_at = datetime.utcnow()
                db.session.commit()
                if first_start:
                    recorder.emit(
                        AgentEventType.RUN_STARTED,
                        message="工作流启动",
                        payload={"workflow": run.workflow, "domain": run.domain},
                    )

                workflow_fn = get_workflow(run.workflow)
                if not workflow_fn:
                    raise RuntimeError(f"Unknown workflow: {run.workflow}")

                ctx = AgentContext(
                    run_id=run.id,
                    user_id=run.user_id,
                    team_id=run.team_id,
                    domain=run.domain,
                    workflow=run.workflow,
                    resource_type=run.resource_type,
                    resource_id=run.resource_id,
                    config=run.get_config(),
                    input_snapshot=run.get_input_snapshot(),
                    is_cancelled=lambda: self.is_cancelled(run_id),
                )

                result = workflow_fn(ctx, recorder) or {}

                run = db.session.get(AgentRun, run_id)
                # Trust the workflow's returned status. The workflow already
                # returns 'cancelled' when it observes a cancel request at a step
                # boundary; a cancel that arrives *after* the workflow finished is
                # intentionally a no-op (the work was completed), so we do not
                # relabel a finished run as cancelled here.
                status = result.get("status", AgentRunStatus.COMPLETED)
                run.status = status
                if result.get("resource_id"):
                    run.resource_id = result["resource_id"]
                # Up-front reservation + any per-call context-verify gate charges
                # the workflow reported (charged as they fired, never refunded).
                run.credit_used = run.credit_reserved + int(result.get("extra_credits", 0) or 0)
                if status == AgentRunStatus.PAUSED:
                    # Human-in-the-loop checkpoint: the workflow produced a document
                    # and is waiting for the user to confirm / adjust. Leave the run
                    # non-terminal — no completed_at, no RUN_COMPLETED, no refund.
                    # The worker exits here; a resume rebuilds state and continues.
                    # The workflow already emitted step_awaiting_review, which ends
                    # the SSE segment.
                    db.session.commit()
                else:
                    run.completed_at = datetime.utcnow()
                    db.session.commit()
                    # Auto-sync the Code session's deliverables to GitHub before the
                    # terminal event so the GITHUB_SYNC events stream live and replay
                    # in order. Non-fatal: failures are recorded by the sync service
                    # and never affect the run's COMPLETED status.
                    if (
                        run.domain == "code"
                        and run.status == AgentRunStatus.COMPLETED
                        and run.resource_id
                    ):
                        try:
                            from backend.services.code.github.sync_service import (
                                autosync_after_run,
                            )

                            autosync_after_run(recorder, run)
                        except Exception:  # noqa: BLE001
                            logger.error(
                                "GitHub autosync failed for run %s", run_id, exc_info=True
                            )
                    recorder.emit(
                        AgentEventType.RUN_COMPLETED,
                        message="工作流结束",
                        payload={"status": run.status, "resource_id": run.resource_id},
                    )
                    _notify_run_finished(run)
            except Exception as exc:  # noqa: BLE001 - persist failure, never crash the worker
                logger.error("Agent run %s failed: %s", run_id, exc, exc_info=True)
                db.session.rollback()
                try:
                    run = db.session.get(AgentRun, run_id)
                    if run:
                        run.status = AgentRunStatus.FAILED
                        run.error_message = str(exc)
                        # Refund the up-front reservation only when the run failed
                        # before producing any artifact (no useful output delivered).
                        # A run that already produced documents/images keeps its charge.
                        if run.credit_reserved and _run_produced_nothing(run_id):
                            try:
                                refund_credits(
                                    run.user_id, run.credit_reserved, "agent_run",
                                    "agent_run", run.id,
                                    description=f"refund failed {run.workflow}",
                                    team_id=run.team_id,
                                )
                                run.credit_used = 0
                            except Exception:  # noqa: BLE001
                                logger.error("Refund failed for run %s", run_id, exc_info=True)
                                run.credit_used = run.credit_reserved
                        else:
                            run.credit_used = run.credit_reserved
                        run.completed_at = datetime.utcnow()
                        db.session.commit()
                except Exception:  # noqa: BLE001
                    logger.error("Failed to mark run %s as failed", run_id, exc_info=True)
                # Emit the error, then the terminal event. The terminal event must
                # fire even if the error emit raises, so the SSE stream ends promptly
                # instead of stalling until the keepalive poll.
                try:
                    recorder.emit(
                        AgentEventType.ERROR,
                        level=AgentEventLevel.ERROR,
                        message=f"工作流失败: {exc}",
                        payload={"error": str(exc)},
                    )
                except Exception:  # noqa: BLE001
                    logger.error("Failed to emit error event for run %s", run_id, exc_info=True)
                try:
                    recorder.emit(
                        AgentEventType.RUN_COMPLETED,
                        message="工作流结束",
                        payload={"status": AgentRunStatus.FAILED},
                    )
                except Exception:  # noqa: BLE001
                    logger.error("Failed to emit run_completed for run %s", run_id, exc_info=True)
                failed_run = db.session.get(AgentRun, run_id)
                if failed_run:
                    _notify_run_finished(failed_run)
            finally:
                self._clear_cancel(run_id)
                # ThreadPoolExecutor reuses threads across runs; reset the
                # thread-scoped session so no state leaks into the next run.
                db.session.remove()


# Process-wide singleton. Worker count is env-tunable: the full-stack pipeline
# fans out THREE concurrent container/model runs per project (frontend + backend
# + middleware), so the default headroom is raised above the original 4 — each
# run mostly blocks on `docker run`, so the slots are I/O-bound, not CPU-bound.
# Default doubled 8 -> 16 to raise overall throughput; each extra slot may spin
# up a Docker build, so size this to the host's CPU/memory.
agent_runtime = AgentRuntime(max_workers=int(os.getenv("AGENT_MAX_WORKERS", "16")))


def _register_builtin_workflows() -> None:
    from backend.services.agent.workflows.code_app_iteration_workflow import (
        run_code_app_iteration_analysis_workflow,
    )
    from backend.services.agent.workflows.code_backend_project_workflow import (
        run_code_backend_project_workflow,
    )
    from backend.services.agent.workflows.code_canvas_workflow import (
        run_code_canvas_generation,
    )
    from backend.services.agent.workflows.code_dev_backend_turn_workflow import (
        run_code_dev_backend_turn_workflow,
    )
    from backend.services.agent.workflows.code_dev_backlog_planner_workflow import (
        run_code_dev_backlog_planner_workflow,
    )
    from backend.services.agent.workflows.code_dev_parallel_turn_workflow import (
        run_code_dev_parallel_turn_workflow,
    )
    from backend.services.agent.workflows.code_dev_sprint_workflow import (
        run_code_dev_sprint_workflow,
    )
    from backend.services.agent.workflows.code_dev_turn_workflow import (
        run_code_dev_turn_workflow,
    )
    from backend.services.agent.workflows.code_figma_slice_workflow import (
        run_code_figma_slice_workflow,
    )
    from backend.services.agent.workflows.code_frontend_project_workflow import (
        run_code_frontend_project_workflow,
    )
    from backend.services.agent.workflows.code_fullstack_deploy_workflow import (
        run_code_fullstack_deploy_workflow,
    )
    from backend.services.agent.workflows.code_middleware_workflow import (
        run_code_middleware_workflow,
    )
    from backend.services.agent.workflows.code_workflow import run_code_workflow

    register_workflow("code_full_generation", run_code_workflow)
    register_workflow(
        "code_app_iteration_analysis", run_code_app_iteration_analysis_workflow
    )
    register_workflow("code_frontend_project_generation", run_code_frontend_project_workflow)
    # Dev Mode: one interactive development turn (bounded run against a long-running
    # dev container). The container lifecycle lives in dev_service, decoupled from the run.
    register_workflow("code_dev_turn", run_code_dev_turn_workflow)
    # Backend dev turn: the backend-lane twin (long-running dev-be container, native
    # hot-reload, contract-driven integration test).
    register_workflow("code_dev_backend_turn", run_code_dev_backend_turn_workflow)
    # Parallel multi-feature dev turn (worktree-isolated fan-out + integration barrier).
    register_workflow("code_dev_parallel_turn", run_code_dev_parallel_turn_workflow)
    # Sprint: the serial task scheduler that feeds the backlog to dev turns one
    # task at a time (DB task state machine = single source of truth).
    register_workflow("code_dev_sprint", run_code_dev_sprint_workflow)
    # Backlog planner (P1): docs+board+goal -> user-confirmable task draft.
    register_workflow("code_dev_backlog_planner", run_code_dev_backlog_planner_workflow)
    register_workflow("code_canvas_generation", run_code_canvas_generation)
    register_workflow("code_figma_slice_generation", run_code_figma_slice_workflow)
    # Full-stack pipeline: backend + middleware generation (concurrent with the
    # frontend project run) and the atomic deploy that joins all three.
    register_workflow("code_backend_project_generation", run_code_backend_project_workflow)
    register_workflow("code_middleware_provisioning", run_code_middleware_workflow)
    register_workflow("code_fullstack_deploy", run_code_fullstack_deploy_workflow)
    # Note: the single-file `code_frontend_generation` and `code_figma_restore`
    # workflows were removed — frontend code is now produced solely by the
    # multi-file project generation; Figma input feeds it via figma_attach_service.


_register_builtin_workflows()
