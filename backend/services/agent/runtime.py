"""
Agent runtime — background execution of workflows.

Mirrors the PPT task manager pattern (a process-wide ThreadPoolExecutor, no
Celery). Each run executes in a worker thread inside ``app.app_context()``. The
runtime owns the cancel registry and the workflow registry; the actual step
logic lives in ``workflows/``.
"""
import logging
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

    def _execute(self, app, run_id: str) -> None:
        with app.app_context():
            recorder = RunRecorder(run_id, event_bus)
            try:
                run = db.session.get(AgentRun, run_id)
                if not run:
                    logger.error("AgentRun %s not found", run_id)
                    return

                run.status = AgentRunStatus.RUNNING
                run.started_at = datetime.utcnow()
                db.session.commit()
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
                run.status = result.get("status", AgentRunStatus.COMPLETED)
                if result.get("resource_id"):
                    run.resource_id = result["resource_id"]
                run.credit_used = run.credit_reserved
                run.completed_at = datetime.utcnow()
                db.session.commit()
                recorder.emit(
                    AgentEventType.RUN_COMPLETED,
                    message="工作流结束",
                    payload={"status": run.status, "resource_id": run.resource_id},
                )
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
            finally:
                self._clear_cancel(run_id)
                # ThreadPoolExecutor reuses threads across runs; reset the
                # thread-scoped session so no state leaks into the next run.
                db.session.remove()


# Process-wide singleton.
agent_runtime = AgentRuntime(max_workers=4)


def _register_builtin_workflows() -> None:
    from backend.services.agent.workflows.code_frontend_workflow import (
        run_code_frontend_workflow,
    )
    from backend.services.agent.workflows.code_workflow import run_code_workflow

    register_workflow("code_full_generation", run_code_workflow)
    register_workflow("code_frontend_generation", run_code_frontend_workflow)


_register_builtin_workflows()
