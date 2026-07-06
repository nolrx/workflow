"""
Dev Mode HTTP endpoints (交互式开发模式).

Manages the interactive dev SESSION (the long-running dev container + persistent
checklist) and starts bounded TURN runs against it. Turns reuse the whole agent
run / SSE / cancel machinery (the frontend attaches to
``GET /api/agent/runs/<id>/stream`` exactly like any other run), so this module
only owns session lifecycle + the checklist board.

Ownership: writes are owner-only (``_owned_project``); reads allow the owner, a
team member, or an admin (``_accessible_project``), matching the App Space gates.
Mounted under ``/api/code``. Comments in English to match the Code convention.
"""
import logging
import os
import re
from datetime import datetime

from flask import Blueprint, Response, current_app, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from backend.extensions import db
from backend.models.agent import AgentRun, AgentRunStatus
from backend.models.code import CodeProject
from backend.models.code.fullstack import (
    CodeDevSession,
    CodeDevSprint,
    CodeDevTask,
    CodeDevTaskPlan,
    DevSessionStatus,
    DevSprintStatus,
    DevTaskPlanStatus,
    DevTaskSource,
    DevTaskStatus,
)
from backend.routes.code.apps_routes import _accessible_project, _owned_project
from backend.services import pricing
from backend.services.agent.runtime import agent_runtime
from backend.services.agent.workflows._iteration_support import load_prior_source
from backend.services.agent.workflows.code_dev_turn_workflow import (
    checklist_board,
    is_runnable_vite,
    load_dev_ledger,
    seed_checklist,
)
from backend.services.code import dev_backlog_planner_service, dev_sprint_service
from backend.services.code.dev_backend_service import get_dev_backend_service
from backend.services.code.dev_service import get_dev_service
from backend.services.credit_service import InsufficientCreditsError, deduct_credits
from backend.services.lifecycle import drain_guard
from backend.utils.preview_token import preview_identity
from backend.utils.response import error_response, success_response

# Cookie that authenticates the preview tab (set by preview_routes on owner entry).
# A backlog-plan stuck in PLANNING/APPLYING longer than this (its model call hung
# or its run was orphaned) is reclaimed so a new generation isn't blocked forever.
_PLAN_STUCK_SECONDS = int(os.getenv("CODE_DEV_PLAN_STUCK_SECONDS", "180"))

_PREVIEW_COOKIE = "fe_preview_token"
_PREVIEW_URI_RE = re.compile(r"^/preview/([^/?]+)")

logger = logging.getLogger(__name__)

dev_bp = Blueprint("code_dev", __name__)


# --- helpers -----------------------------------------------------------------
def _dev_service_for(lane: str):
    """The dev container service for a session lane (frontend Vite / backend hot-reload)."""
    return get_dev_backend_service() if lane == "backend" else get_dev_service()


def _turn_workflow_for(lane: str) -> str:
    """The turn workflow key for a session lane."""
    return "code_dev_backend_turn" if lane == "backend" else "code_dev_turn"


# All turn workflow keys per lane (frontend has single + parallel turns) — used to
# find the run a refreshed page should REATTACH to.
_TURN_WORKFLOWS = {
    "frontend": ["code_dev_turn", "code_dev_parallel_turn"],
    "backend": ["code_dev_backend_turn"],
}


def _active_or_latest_turn_run_id(project_id: str, user_id: str, lane: str) -> str | None:
    """The run a refreshed dev page should reattach to: the IN-FLIGHT turn if one is
    running (so the live process/result keep streaming after a reload), else the most
    recent turn (to replay its stored events — the last result). Covers single +
    parallel turns."""
    wfs = _TURN_WORKFLOWS.get(lane, ["code_dev_turn"])
    base = AgentRun.query.filter(
        AgentRun.resource_id == project_id,
        AgentRun.user_id == user_id,
        AgentRun.workflow.in_(wfs),
    )
    active = (
        base.filter(AgentRun.status.in_(list(AgentRunStatus.ACTIVE)))
        .order_by(AgentRun.created_at.desc())
        .first()
    )
    if active:
        return active.id
    latest = base.order_by(AgentRun.created_at.desc()).first()
    return latest.id if latest else None


def _snapshot_source_for(lane: str, run_id: str, project_id: str, files: dict) -> bool:
    """Persist the session's source as the lane-appropriate durable artifact."""
    if lane == "backend":
        from backend.services.agent.workflows.code_dev_backend_turn_workflow import (
            persist_backend_snapshot_standalone,
        )

        return persist_backend_snapshot_standalone(run_id, project_id, files)
    from backend.services.agent.workflows.code_dev_turn_workflow import (
        persist_snapshot_standalone,
    )

    return persist_snapshot_standalone(run_id, project_id, files)


def _start_dev_run(
    user_id, team_id, project_id: str, config: dict, workflow: str = "code_dev_turn",
    cost: int | None = None,
) -> AgentRun | None:
    """Reserve credits and start one Dev Mode run (mirrors _start_run).

    Returns the started run, or ``None`` on insufficient credits. Uses the Dev Mode
    price directly (not _cost_for, which doesn't know this workflow). ``workflow`` is
    ``code_dev_turn`` (single), ``code_dev_parallel_turn`` (multi-lane) or
    ``code_dev_sprint`` (the serial task scheduler)."""
    cost = pricing.CODE_DEV_TURN if cost is None else cost
    run = AgentRun(
        user_id=user_id,
        team_id=team_id,
        domain="code",
        workflow=workflow,
        resource_type="code_project",
        resource_id=project_id,
        title=config.get("title"),
        status=AgentRunStatus.QUEUED,
        credit_reserved=cost,
    )
    run.set_config(config)
    run.set_input_snapshot({
        "domain": "code",
        "workflow": workflow,
        "resource_type": "code_project",
        "resource_id": project_id,
        "config": config,
    })
    db.session.add(run)
    db.session.commit()
    if cost > 0:
        try:
            deduct_credits(
                user_id=user_id,
                amount=cost,
                operation="agent_run",
                resource_type="agent_run",
                resource_id=run.id,
                description=f"Agent run: {workflow}",
                team_id=team_id,
            )
        except InsufficientCreditsError:
            db.session.delete(run)
            db.session.commit()
            return None
    agent_runtime.start(current_app._get_current_object(), run.id)
    return run


def _active_session(project_id: str, user_id: str, lane: str = "frontend") -> CodeDevSession | None:
    return (
        CodeDevSession.query.filter_by(project_id=project_id, user_id=user_id, lane=lane)
        .filter(CodeDevSession.status.in_(list(DevSessionStatus.ACTIVE)))
        .order_by(CodeDevSession.created_at.desc())
        .first()
    )


def _latest_turn_run_id(project_id: str, user_id: str, lane: str = "frontend") -> str | None:
    run = (
        AgentRun.query.filter(
            AgentRun.resource_id == project_id,
            AgentRun.user_id == user_id,
            AgentRun.workflow.in_(_TURN_WORKFLOWS.get(lane, ["code_dev_turn"])),
        )
        .order_by(AgentRun.created_at.desc())
        .first()
    )
    return run.id if run else None


def _reconcile_session(session: CodeDevSession) -> None:
    """Lazily sync the session's status/health with the actual container state."""
    from backend.services.code.dev_maintenance import reconcile_session

    if reconcile_session(session):
        db.session.commit()


def _session_view(session: CodeDevSession, project_id: str, user_id: str) -> dict:
    sprint = (
        CodeDevSprint.query.filter_by(session_id=session.id)
        .order_by(CodeDevSprint.created_at.desc())
        .first()
    )
    return {
        "session": session.to_dict(),
        "board": checklist_board(session.id),
        "latest_run_id": _latest_turn_run_id(project_id, user_id, session.lane),
        # The live sprint (if any) so a refreshed page can reattach its scheduler view.
        "sprint": sprint.to_dict() if sprint and sprint.status not in DevSprintStatus.TERMINAL else None,
    }


# --- endpoints ---------------------------------------------------------------
@dev_bp.route("/projects/<project_id>/dev-sessions", methods=["POST"])
@jwt_required()
def start_session(project_id: str):
    """Start (or resume) the interactive dev session for a project.

    Creates the CodeDevSession + seeds the checklist from the ledger, then kicks off
    a bootstrap turn (which starts the long-running dev container; and, when the
    project has no prior frontend source, scaffolds the skeleton)."""
    drained = drain_guard()
    if drained:
        return drained
    user_id = get_jwt_identity()
    project = _owned_project(project_id, user_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)

    dev = get_dev_service()
    if not dev.is_available():
        return error_response(
            "SERVER_ERROR", "开发模式不可用：未配置容器运行时或 Anthropic 凭证", 503
        )

    existing = _active_session(project_id, user_id)
    if existing:
        _reconcile_session(existing)  # docker inspect: flips to STOPPED if container gone
        # Container still alive → REATTACH to the in-flight (or last) turn so a page
        # refresh keeps streaming the live process/result. Spawning a bootstrap run
        # here (the old behaviour) would make ITSELF the "latest" run and hide the
        # in-flight turn → the user would lose all awareness of the running execution.
        if existing.status in DevSessionStatus.ACTIVE:
            attach = _active_or_latest_turn_run_id(project_id, user_id, existing.lane)
            if attach:
                return success_response(
                    {**_session_view(existing, project_id, user_id), "run_id": attach}
                )
        # Container vanished (idle-reaped / crashed) → revive + bootstrap to bring the
        # dev server back up (reuses the same session + checklist).
        if existing.status in DevSessionStatus.TERMINAL:
            existing.status = DevSessionStatus.STARTING
            db.session.commit()
        run = _start_dev_run(
            user_id, project.team_id, project_id,
            {"session_id": existing.id, "instruction": "", "bootstrap": True, "title": "开发会话恢复"},
        )
        if run is None:
            return error_response("INSUFFICIENT_CREDITS", "积分不足", 402)
        return success_response({**_session_view(existing, project_id, user_id), "run_id": run.id})

    session = CodeDevSession(
        project_id=project_id,
        user_id=user_id,
        team_id=project.team_id,
        lane="frontend",
        status=DevSessionStatus.STARTING,
        preview_path=f"/preview/{project_id}/",
    )
    db.session.add(session)
    db.session.commit()

    # Seed the persistent checklist from the consensus ledger (session-first).
    ledger = load_dev_ledger(session, project)
    session.set_shared_ledger(ledger.to_dict())
    db.session.commit()
    seed_checklist(session.id, project_id, ledger.to_dict())

    # Prior RUNNABLE frontend project → use its code directly (dev container is
    # seeded with it via _resolve_source) and AUDIT it once: the bootstrap turn runs
    # the acceptance review against the existing repo so the checklist is calibrated
    # to what's ALREADY implemented. A non-runnable / scaffold-only prior artifact (or
    # none) → scaffold the skeleton instead.
    try:
        prior = load_prior_source(project_id, "frontend")
    except Exception:  # noqa: BLE001
        prior = {}
    has_code = is_runnable_vite(prior)
    if has_code:
        boot_cfg = {
            "session_id": session.id, "instruction": "", "bootstrap": True,
            "audit": True, "title": "开发会话启动 · 校准功能清单",
        }
    else:
        boot_cfg = {
            "session_id": session.id, "bootstrap": True, "title": "开发会话启动",
            "instruction": (
                "初始化项目框架:严格按既定需求文档/风格文档搭建 React + Vite + TypeScript 的目录骨架"
                "与关键文件桩,确保 dev server 能正常启动;本轮只搭骨架,不实现完整功能(功能将在后续对话回合逐步实现)。"
            ),
        }
    run = _start_dev_run(user_id, project.team_id, project_id, boot_cfg)
    if run is None:
        db.session.delete(session)
        db.session.commit()
        return error_response("INSUFFICIENT_CREDITS", "积分不足", 402)
    return success_response(
        {**_session_view(session, project_id, user_id), "run_id": run.id}, message="开发会话已启动"
    )


@dev_bp.route("/projects/<project_id>/dev-sessions/<session_id>", methods=["GET"])
@jwt_required()
def get_session(project_id: str, session_id: str):
    user_id = get_jwt_identity()
    if not _accessible_project(project_id, user_id):
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)
    session = db.session.get(CodeDevSession, session_id)
    if not session or session.project_id != project_id:
        return error_response("NOT_FOUND", "开发会话不存在", 404)
    _reconcile_session(session)
    return success_response(_session_view(session, project_id, session.user_id))


@dev_bp.route("/projects/<project_id>/dev-sessions/<session_id>/checklist", methods=["GET"])
@jwt_required()
def get_checklist(project_id: str, session_id: str):
    user_id = get_jwt_identity()
    if not _accessible_project(project_id, user_id):
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)
    session = db.session.get(CodeDevSession, session_id)
    if not session or session.project_id != project_id:
        return error_response("NOT_FOUND", "开发会话不存在", 404)
    return success_response({"board": checklist_board(session.id)})


@dev_bp.route("/projects/<project_id>/dev-sessions/<session_id>/logs", methods=["GET"])
@jwt_required()
def get_logs(project_id: str, session_id: str):
    """Read-only tail of the dev container's logs — ALL types (stdout+stderr merged:
    install / dev-server / runtime / errors / access logs), timestamped. Lane-aware:
    resolves the frontend Vite container OR the backend hot-reload container."""
    user_id = get_jwt_identity()
    if not _accessible_project(project_id, user_id):
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)
    session = db.session.get(CodeDevSession, session_id)
    if not session or session.project_id != project_id:
        return error_response("NOT_FOUND", "开发会话不存在", 404)
    try:
        tail = int(request.args.get("tail", 500))
    except (TypeError, ValueError):
        tail = 500
    dev = _dev_service_for(session.lane)
    logs = dev.container_logs(project_id, tail=tail, timestamps=True)
    return success_response({
        "available": bool(logs),
        "logs": logs,
        "container": session.container_name,
        "lane": session.lane,
    })


@dev_bp.route("/projects/<project_id>/dev-sessions/<session_id>/turns", methods=["POST"])
@jwt_required()
def start_turn(project_id: str, session_id: str):
    """Start one interactive development turn (edit + verify + checklist)."""
    drained = drain_guard()
    if drained:
        return drained
    user_id = get_jwt_identity()
    project = _owned_project(project_id, user_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)
    session = db.session.get(CodeDevSession, session_id)
    if not session or session.project_id != project_id or session.user_id != user_id:
        return error_response("NOT_FOUND", "开发会话不存在", 404)
    if session.status in DevSessionStatus.TERMINAL:
        return error_response("VALIDATION_ERROR", "开发会话已停止，请重新启动开发模式", 400)

    data = request.get_json() or {}
    instruction = (data.get("instruction") or "").strip()
    if not instruction:
        return error_response("VALIDATION_ERROR", "请描述本回合要开发/修改的内容", 400)

    run = _start_dev_run(
        user_id, project.team_id, project_id,
        {"session_id": session.id, "instruction": instruction, "title": instruction[:80]},
        workflow=_turn_workflow_for(session.lane),
    )
    if run is None:
        return error_response("INSUFFICIENT_CREDITS", "积分不足", 402)
    return success_response({"run_id": run.id}, message="开发回合已开始")


@dev_bp.route("/projects/<project_id>/dev-sessions/<session_id>/parallel-turns", methods=["POST"])
@jwt_required()
def start_parallel_turn(project_id: str, session_id: str):
    """Start ONE parallel multi-feature dev turn (N isolated subagents + integration).

    Body: ``{"lanes": [{"instruction": "...", "feature_ids": ["FR-01"]}, ...]}``. Each
    lane develops an independent feature in its own git worktree; a mandatory
    integration barrier merges them back (conflicts re-applied serially)."""
    drained = drain_guard()
    if drained:
        return drained
    user_id = get_jwt_identity()
    project = _owned_project(project_id, user_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)
    session = db.session.get(CodeDevSession, session_id)
    if not session or session.project_id != project_id or session.user_id != user_id:
        return error_response("NOT_FOUND", "开发会话不存在", 404)
    if session.status in DevSessionStatus.TERMINAL:
        return error_response("VALIDATION_ERROR", "开发会话已停止，请重新启动开发模式", 400)

    data = request.get_json() or {}
    raw = data.get("lanes") or []
    lanes = [
        {"instruction": (ln.get("instruction") or "").strip(), "feature_ids": ln.get("feature_ids") or []}
        for ln in raw
        if isinstance(ln, dict) and (ln.get("instruction") or "").strip()
    ]
    if not lanes:
        return error_response("VALIDATION_ERROR", "请至少提供一个并行开发分片", 400)

    run = _start_dev_run(
        user_id, project.team_id, project_id,
        {"session_id": session.id, "lanes": lanes, "title": f"并行开发 ×{len(lanes)}"},
        workflow="code_dev_parallel_turn",
    )
    if run is None:
        return error_response("INSUFFICIENT_CREDITS", "积分不足", 402)
    return success_response({"run_id": run.id, "lanes": len(lanes)}, message="并行开发已开始")


@dev_bp.route("/projects/<project_id>/dev-sessions/<session_id>/tasks", methods=["POST"])
@jwt_required()
def add_task(project_id: str, session_id: str):
    """Add a user-authored checklist item (功能点)."""
    user_id = get_jwt_identity()
    if not _owned_project(project_id, user_id):
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)
    session = db.session.get(CodeDevSession, session_id)
    if not session or session.project_id != project_id or session.user_id != user_id:
        return error_response("NOT_FOUND", "开发会话不存在", 404)
    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    if not title:
        return error_response("VALIDATION_ERROR", "功能点标题不能为空", 400)
    from sqlalchemy import func

    order = db.session.query(func.max(CodeDevTask.order_index)).filter_by(
        session_id=session_id
    ).scalar() or 0
    task = CodeDevTask(
        project_id=project_id,
        session_id=session_id,
        feature_id=None,
        category=(data.get("category") or "functional"),
        title=title[:300],
        description=(data.get("description") or None),
        status=DevTaskStatus.PENDING,
        source=DevTaskSource.USER_ADDED,
        order_index=order + 1,
    )
    db.session.add(task)
    db.session.commit()
    return success_response({"task": task.to_dict(), "board": checklist_board(session_id)})


@dev_bp.route("/projects/<project_id>/dev-tasks/<task_id>", methods=["PATCH"])
@jwt_required()
def update_task(project_id: str, task_id: str):
    """User edit of a checklist item (status / title / description / note)."""
    user_id = get_jwt_identity()
    if not _owned_project(project_id, user_id):
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)
    task = db.session.get(CodeDevTask, task_id)
    if not task or task.project_id != project_id:
        return error_response("NOT_FOUND", "功能点不存在", 404)
    data = request.get_json() or {}
    if "status" in data:
        status = data.get("status")
        # Scheduler-owned states (queued/verifying/blocked/failed/cancelled) are
        # only ever written by the sprint state machine; the user vocabulary is
        # the original checklist set. Setting pending also clears a block.
        if status not in DevTaskStatus.USER_SETTABLE:
            return error_response("VALIDATION_ERROR", "非法的状态值", 400)
        task.status = status
        if status == DevTaskStatus.PENDING:
            task.blocked_reason = None
    if "title" in data and (data.get("title") or "").strip():
        task.title = data["title"].strip()[:300]
    if "description" in data:
        task.description = data.get("description")
    if "note" in data:
        task.note = data.get("note")
    if "acceptance_criteria" in data and isinstance(data.get("acceptance_criteria"), list):
        task.set_acceptance_criteria([str(c)[:500] for c in data["acceptance_criteria"][:20]])
    if "depends_on" in data and isinstance(data.get("depends_on"), list):
        task.set_depends_on([str(d)[:60] for d in data["depends_on"][:20]])
    if "priority" in data:
        try:
            task.priority = int(data.get("priority"))
        except (TypeError, ValueError):
            pass
    db.session.commit()
    return success_response({"task": task.to_dict(), "board": checklist_board(task.session_id)})


# --- sprint backlog + scheduler (P0: serial) ----------------------------------
def _owned_session(project_id: str, session_id: str, user_id: str) -> CodeDevSession | None:
    session = db.session.get(CodeDevSession, session_id)
    if not session or session.project_id != project_id or session.user_id != user_id:
        return None
    return session


@dev_bp.route("/projects/<project_id>/dev-sessions/<session_id>/tasks/bulk", methods=["POST"])
@jwt_required()
def bulk_tasks(project_id: str, session_id: str):
    """Bulk-write the session's task backlog (the sprint's input).

    Body: ``{"tasks": [{title, feature_id?, parent_feature_id?, lane?, category?,
    description?, acceptance_criteria?, depends_on?, resource_spec?, priority?,
    max_retries?}], "replace": bool}``. Append mode upserts by ``feature_id``
    (delivered / in-flight rows are never clobbered); replace mode swaps the whole
    board and is refused while a sprint is active or any task is in flight."""
    user_id = get_jwt_identity()
    if not _owned_project(project_id, user_id):
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)
    session = _owned_session(project_id, session_id, user_id)
    if not session:
        return error_response("NOT_FOUND", "开发会话不存在", 404)

    data = request.get_json() or {}
    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return error_response("VALIDATION_ERROR", "tasks 不能为空", 400)
    if len(raw_tasks) > 200:
        return error_response("VALIDATION_ERROR", "单次最多写入 200 个任务", 400)
    replace = bool(data.get("replace"))

    normalized: list[dict] = []
    for i, rt in enumerate(raw_tasks):
        if not isinstance(rt, dict) or not (rt.get("title") or "").strip():
            return error_response("VALIDATION_ERROR", f"第 {i + 1} 个任务缺少标题", 400)
        criteria = rt.get("acceptance_criteria")
        deps = rt.get("depends_on")
        spec = rt.get("resource_spec")
        try:
            priority = int(rt["priority"]) if rt.get("priority") is not None else None
        except (TypeError, ValueError):
            priority = None
        try:
            max_retries = (
                max(0, min(5, int(rt["max_retries"]))) if rt.get("max_retries") is not None else None
            )
        except (TypeError, ValueError):
            max_retries = None
        normalized.append({
            "feature_id": (str(rt["feature_id"]).strip()[:60] or None) if rt.get("feature_id") else None,
            "parent_feature_id": (
                (str(rt["parent_feature_id"]).strip()[:60] or None)
                if rt.get("parent_feature_id") else None
            ),
            "lane": dev_sprint_service.normalize_lane(rt.get("lane")),
            "category": dev_sprint_service.normalize_category(rt.get("category")),
            "title": rt["title"].strip()[:300],
            "description": rt.get("description") or None,
            "acceptance_criteria": (
                [str(c)[:500] for c in criteria[:20]] if isinstance(criteria, list) else []
            ),
            "depends_on": [str(d)[:60] for d in deps[:20]] if isinstance(deps, list) else [],
            "resource_spec": spec if isinstance(spec, dict) else {},
            "priority": priority,
            "max_retries": max_retries,
        })

    try:
        counts = dev_sprint_service.bulk_write_tasks(
            project_id, session_id, normalized, replace=replace,
        )
    except dev_sprint_service.BulkWriteRefused as exc:
        return error_response("VALIDATION_ERROR", str(exc), 400)
    return success_response({**counts, "board": checklist_board(session_id)})


# --- backlog planner (P1): AI task drafts, user-confirmed before they hit the board --
def _get_plan(project_id: str, session_id: str, plan_id: str) -> CodeDevTaskPlan | None:
    plan = db.session.get(CodeDevTaskPlan, plan_id)
    if not plan or plan.project_id != project_id or plan.session_id != session_id:
        return None
    return plan


@dev_bp.route("/projects/<project_id>/dev-sessions/<session_id>/task-plans", methods=["POST"])
@jwt_required()
def create_task_plan(project_id: str, session_id: str):
    """Generate a task draft from the project docs (one planner run; draft only —
    the board is untouched until an explicit apply)."""
    drained = drain_guard()
    if drained:
        return drained
    user_id = get_jwt_identity()
    project = _owned_project(project_id, user_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)
    session = _owned_session(project_id, session_id, user_id)
    if not session:
        return error_response("NOT_FOUND", "开发会话不存在", 404)
    if session.lane != "frontend":
        return error_response("VALIDATION_ERROR", "任务规划当前仅支持前端开发会话", 400)
    # Only a GENUINELY in-flight plan blocks a new one. Reclaim a stuck/orphaned
    # plan (its run finished/vanished without advancing the plan, or the model call
    # hung far past a reasonable budget) so a hung generation can't permanently
    # block the user — otherwise a single flaky gateway call bricks the planner.
    for stuck in (
        CodeDevTaskPlan.query.filter_by(session_id=session_id)
        .filter(CodeDevTaskPlan.status.in_([DevTaskPlanStatus.PLANNING, DevTaskPlanStatus.APPLYING]))
        .all()
    ):
        run = db.session.get(AgentRun, stuck.run_id) if stuck.run_id else None
        run_active = run is not None and run.status in AgentRunStatus.ACTIVE
        age = (datetime.utcnow() - (stuck.updated_at or stuck.created_at)).total_seconds()
        if run_active and age < _PLAN_STUCK_SECONDS:
            return error_response("VALIDATION_ERROR", "已有正在生成/应用中的任务计划", 400)
        # Reclaim: cancel a hung run (best-effort) and fail the plan so publish is a
        # no-op (the workflow's publish step only advances a still-PLANNING plan).
        if run_active:
            agent_runtime.request_cancel(stuck.run_id)
        stuck.status = DevTaskPlanStatus.FAILED
        stuck.error_message = stuck.error_message or "上次规划生成超时/中断,已回收"
        db.session.commit()

    data = request.get_json(silent=True) or {}
    lanes = data.get("target_lanes")
    lanes = [str(x) for x in lanes] if isinstance(lanes, list) else None
    try:
        max_tasks = max(1, min(200, int(data["max_tasks"]))) if data.get("max_tasks") else None
    except (TypeError, ValueError):
        max_tasks = None
    plan = CodeDevTaskPlan(
        project_id=project_id,
        session_id=session_id,
        status=DevTaskPlanStatus.PLANNING,
        mode=(str(data.get("mode") or "from_project"))[:30],
        created_by=user_id,
    )
    if lanes:
        plan.set_target_lanes(lanes)
    db.session.add(plan)
    db.session.commit()
    run = _start_dev_run(
        user_id, project.team_id, project_id,
        {
            "session_id": session_id, "plan_id": plan.id,
            "target_lanes": lanes, "include_assets": data.get("include_assets", True),
            "max_tasks": max_tasks, "instruction": (data.get("instruction") or "").strip()[:2000],
            "title": "生成任务列表",
        },
        workflow="code_dev_backlog_planner", cost=pricing.CODE_DEV_BACKLOG_PLANNER,
    )
    if run is None:
        db.session.delete(plan)
        db.session.commit()
        return error_response("INSUFFICIENT_CREDITS", "积分不足", 402)
    plan.run_id = run.id
    db.session.commit()
    return success_response(
        {"plan": plan.to_dict(include_plan=False), "run_id": run.id},
        message="任务规划已开始",
    )


@dev_bp.route("/projects/<project_id>/dev-sessions/<session_id>/task-plans", methods=["GET"])
@jwt_required()
def list_task_plans(project_id: str, session_id: str):
    user_id = get_jwt_identity()
    if not _accessible_project(project_id, user_id):
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)
    plans = (
        CodeDevTaskPlan.query.filter_by(session_id=session_id, project_id=project_id)
        .order_by(CodeDevTaskPlan.created_at.desc())
        .limit(20)
        .all()
    )
    return success_response({"plans": [p.to_dict(include_plan=False) for p in plans]})


@dev_bp.route(
    "/projects/<project_id>/dev-sessions/<session_id>/task-plans/<plan_id>", methods=["GET"]
)
@jwt_required()
def get_task_plan(project_id: str, session_id: str, plan_id: str):
    user_id = get_jwt_identity()
    project = _accessible_project(project_id, user_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)
    plan = _get_plan(project_id, session_id, plan_id)
    if not plan:
        return error_response("NOT_FOUND", "任务计划不存在", 404)
    session = db.session.get(CodeDevSession, session_id)
    # Lazily surface staleness so the UI can prompt a regenerate before apply fails.
    if session:
        dev_backlog_planner_service.check_staleness(plan, project, session)
    return success_response({"plan": plan.to_dict(), "run_id": plan.run_id})


@dev_bp.route(
    "/projects/<project_id>/dev-sessions/<session_id>/task-plans/<plan_id>", methods=["PATCH"]
)
@jwt_required()
def update_task_plan(project_id: str, session_id: str, plan_id: str):
    """User edit of a DRAFT plan (tasks / summary / assumptions). The edited tasks
    are re-normalized so a hand edit can't smuggle in a cycle or a bad lane."""
    user_id = get_jwt_identity()
    if not _owned_project(project_id, user_id):
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)
    plan = _get_plan(project_id, session_id, plan_id)
    if not plan:
        return error_response("NOT_FOUND", "任务计划不存在", 404)
    if plan.status != DevTaskPlanStatus.DRAFT:
        return error_response("VALIDATION_ERROR", f"仅草稿可编辑(当前 {plan.status})", 400)

    data = request.get_json(silent=True) or {}
    current = plan.get_plan()
    edited = dict(current)
    if isinstance(data.get("tasks"), list):
        edited["tasks"] = data["tasks"]
    if "summary" in data:
        edited["summary"] = str(data.get("summary") or "")[:1000]
    if isinstance(data.get("assumptions"), list):
        edited["assumptions"] = [str(a)[:300] for a in data["assumptions"]][:12]
    existing = dev_sprint_service.session_tasks(session_id)
    request_meta = current.get("request") or {}
    normalized, warnings = dev_backlog_planner_service.normalize_plan(
        edited, existing_tasks=existing,
        max_tasks=int(request_meta.get("max_tasks") or dev_backlog_planner_service.DEFAULT_MAX_TASKS),
    )
    if not normalized["tasks"]:
        return error_response("VALIDATION_ERROR", "编辑后的计划没有任何合法任务", 400)
    normalized["request"] = request_meta
    plan.set_plan(normalized)
    # System warnings are the normalizer's audit trail — append, never clear.
    user_warnings = [str(w)[:300] for w in data.get("append_warnings") or [] if str(w).strip()]
    plan.set_warnings(plan.get_warnings() + warnings + user_warnings)
    db.session.commit()
    return success_response({"plan": plan.to_dict()})


@dev_bp.route(
    "/projects/<project_id>/dev-sessions/<session_id>/task-plans/<plan_id>/apply",
    methods=["POST"],
)
@jwt_required()
def apply_task_plan(project_id: str, session_id: str, plan_id: str):
    """Fold a confirmed draft onto the task board (same guarded path as tasks/bulk)."""
    user_id = get_jwt_identity()
    project = _owned_project(project_id, user_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)
    session = _owned_session(project_id, session_id, user_id)
    if not session:
        return error_response("NOT_FOUND", "开发会话不存在", 404)
    plan = _get_plan(project_id, session_id, plan_id)
    if not plan:
        return error_response("NOT_FOUND", "任务计划不存在", 404)

    data = request.get_json(silent=True) or {}
    try:
        counts = dev_backlog_planner_service.apply_plan(
            plan, project, session,
            replace=bool(data.get("replace")), force=bool(data.get("force")),
        )
    except dev_backlog_planner_service.PlanStale as exc:
        return error_response("CONFLICT", str(exc), 409)
    except dev_backlog_planner_service.PlanNotApplicable as exc:
        return error_response("VALIDATION_ERROR", str(exc), 400)
    except dev_sprint_service.BulkWriteRefused as exc:
        return error_response("VALIDATION_ERROR", str(exc), 400)
    return success_response(
        {**counts, "plan": plan.to_dict(include_plan=False), "board": checklist_board(session_id)},
        message="任务草案已应用到任务板",
    )


@dev_bp.route(
    "/projects/<project_id>/dev-sessions/<session_id>/task-plans/<plan_id>/reject",
    methods=["POST"],
)
@jwt_required()
def reject_task_plan(project_id: str, session_id: str, plan_id: str):
    user_id = get_jwt_identity()
    if not _owned_project(project_id, user_id):
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)
    plan = _get_plan(project_id, session_id, plan_id)
    if not plan:
        return error_response("NOT_FOUND", "任务计划不存在", 404)
    if plan.status in DevTaskPlanStatus.TERMINAL and plan.status != DevTaskPlanStatus.STALE:
        return success_response({"plan": plan.to_dict(include_plan=False)})
    plan.status = DevTaskPlanStatus.REJECTED
    db.session.commit()
    return success_response({"plan": plan.to_dict(include_plan=False)}, message="已放弃该任务草案")


@dev_bp.route("/projects/<project_id>/dev-sessions/<session_id>/sprints", methods=["POST"])
@jwt_required()
def create_sprint(project_id: str, session_id: str):
    """Create AND start one sprint: a serial scheduling run over the task backlog."""
    drained = drain_guard()
    if drained:
        return drained
    user_id = get_jwt_identity()
    project = _owned_project(project_id, user_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)
    session = _owned_session(project_id, session_id, user_id)
    if not session:
        return error_response("NOT_FOUND", "开发会话不存在", 404)
    if session.status in DevSessionStatus.TERMINAL:
        return error_response("VALIDATION_ERROR", "开发会话已停止，请重新启动开发模式", 400)
    if session.lane != "frontend":
        return error_response("VALIDATION_ERROR", "当前 Sprint 仅支持前端开发会话", 400)

    data = request.get_json() or {}
    mode = (data.get("mode") or "serial").strip()
    if mode != "serial":
        return error_response("VALIDATION_ERROR", "当前仅支持 serial 模式（并行调度将在后续版本开放）", 400)
    existing = (
        CodeDevSprint.query.filter_by(session_id=session_id)
        .filter(~CodeDevSprint.status.in_(list(DevSprintStatus.TERMINAL)))
        .first()
    )
    if existing:
        return error_response("VALIDATION_ERROR", "已有进行中的 Sprint（可暂停/取消后再新建）", 400)
    pending = CodeDevTask.query.filter_by(
        session_id=session_id, status=DevTaskStatus.PENDING
    ).count()
    if not pending:
        return error_response("VALIDATION_ERROR", "任务板没有待执行任务，请先写入任务", 400)
    try:
        max_turns = max(1, min(200, int(data["max_turns"]))) if data.get("max_turns") else None
    except (TypeError, ValueError):
        max_turns = None

    sprint = CodeDevSprint(
        project_id=project_id,
        session_id=session_id,
        lane=session.lane,
        mode="serial",
        status=DevSprintStatus.PLANNED,
        max_turns=max_turns,
        created_by=user_id,
    )
    db.session.add(sprint)
    db.session.commit()
    run = _start_dev_run(
        user_id, project.team_id, project_id,
        {"session_id": session_id, "sprint_id": sprint.id,
         "title": f"Sprint · {pending} 项任务"},
        workflow="code_dev_sprint", cost=pricing.CODE_DEV_SPRINT,
    )
    if run is None:
        db.session.delete(sprint)
        db.session.commit()
        return error_response("INSUFFICIENT_CREDITS", "积分不足", 402)
    sprint.run_id = run.id
    db.session.commit()
    return success_response(
        {"sprint": sprint.to_dict(), "run_id": run.id}, message="Sprint 已启动"
    )


def _get_sprint(project_id: str, session_id: str, sprint_id: str) -> CodeDevSprint | None:
    sprint = db.session.get(CodeDevSprint, sprint_id)
    if not sprint or sprint.project_id != project_id or sprint.session_id != session_id:
        return None
    return sprint


@dev_bp.route(
    "/projects/<project_id>/dev-sessions/<session_id>/sprints/<sprint_id>", methods=["GET"]
)
@jwt_required()
def get_sprint(project_id: str, session_id: str, sprint_id: str):
    user_id = get_jwt_identity()
    if not _accessible_project(project_id, user_id):
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)
    sprint = _get_sprint(project_id, session_id, sprint_id)
    if not sprint:
        return error_response("NOT_FOUND", "Sprint 不存在", 404)
    return success_response({
        "sprint": sprint.to_dict(),
        "board": checklist_board(session_id),
        "run_id": sprint.run_id,
    })


@dev_bp.route(
    "/projects/<project_id>/dev-sessions/<session_id>/sprints/<sprint_id>/pause",
    methods=["POST"],
)
@jwt_required()
def pause_sprint(project_id: str, session_id: str, sprint_id: str):
    """Request a pause; the scheduler finishes the current turn then parks (paused)."""
    user_id = get_jwt_identity()
    if not _owned_project(project_id, user_id):
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)
    sprint = _get_sprint(project_id, session_id, sprint_id)
    if not sprint:
        return error_response("NOT_FOUND", "Sprint 不存在", 404)
    if sprint.status in DevSprintStatus.TERMINAL:
        return error_response("VALIDATION_ERROR", f"Sprint 已结束（{sprint.status}）", 400)
    if sprint.status in (DevSprintStatus.RUNNING, DevSprintStatus.PLANNED):
        sprint.status = DevSprintStatus.PAUSING
        db.session.commit()
    return success_response({"sprint": sprint.to_dict()}, message="将于当前回合结束后暂停")


@dev_bp.route(
    "/projects/<project_id>/dev-sessions/<session_id>/sprints/<sprint_id>/resume",
    methods=["POST"],
)
@jwt_required()
def resume_sprint(project_id: str, session_id: str, sprint_id: str):
    drained = drain_guard()
    if drained:
        return drained
    user_id = get_jwt_identity()
    project = _owned_project(project_id, user_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)
    sprint = _get_sprint(project_id, session_id, sprint_id)
    if not sprint:
        return error_response("NOT_FOUND", "Sprint 不存在", 404)
    if sprint.status == DevSprintStatus.PAUSING:
        # Pause not yet taken effect — just withdraw it; the live run keeps going.
        sprint.status = DevSprintStatus.RUNNING
        db.session.commit()
        return success_response({"sprint": sprint.to_dict()}, message="已撤回暂停请求")
    if sprint.status != DevSprintStatus.PAUSED:
        return error_response("VALIDATION_ERROR", f"Sprint 当前状态（{sprint.status}）不可恢复", 400)
    sprint.status = DevSprintStatus.RUNNING
    run = db.session.get(AgentRun, sprint.run_id) if sprint.run_id else None
    if run and run.status == AgentRunStatus.PAUSED:
        db.session.commit()
        agent_runtime.start(current_app._get_current_object(), run.id)
        return success_response(
            {"sprint": sprint.to_dict(), "run_id": run.id}, message="Sprint 已恢复"
        )
    # The orchestrator run is gone/terminal (e.g. restart cap) — bind a fresh one;
    # the scheduler is stateless so it simply re-enters and continues.
    new_run = _start_dev_run(
        user_id, project.team_id, project_id,
        {"session_id": session_id, "sprint_id": sprint.id, "title": "Sprint · 恢复"},
        workflow="code_dev_sprint", cost=pricing.CODE_DEV_SPRINT,
    )
    if new_run is None:
        sprint.status = DevSprintStatus.PAUSED
        db.session.commit()
        return error_response("INSUFFICIENT_CREDITS", "积分不足", 402)
    sprint.run_id = new_run.id
    db.session.commit()
    return success_response(
        {"sprint": sprint.to_dict(), "run_id": new_run.id}, message="Sprint 已恢复"
    )


@dev_bp.route(
    "/projects/<project_id>/dev-sessions/<session_id>/sprints/<sprint_id>/cancel",
    methods=["POST"],
)
@jwt_required()
def cancel_sprint(project_id: str, session_id: str, sprint_id: str):
    """Cancel the sprint: stop the scheduler + the in-flight turn, release claims."""
    from datetime import datetime

    user_id = get_jwt_identity()
    if not _owned_project(project_id, user_id):
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)
    sprint = _get_sprint(project_id, session_id, sprint_id)
    if not sprint:
        return error_response("NOT_FOUND", "Sprint 不存在", 404)
    if sprint.status in DevSprintStatus.TERMINAL:
        return success_response({"sprint": sprint.to_dict()})
    current_ids = sprint.get_current_task_ids()
    sprint.status = DevSprintStatus.CANCELLED
    sprint.finished_at = datetime.utcnow()
    db.session.commit()
    # Stop the orchestrator + forward to the in-flight child turn; release claims
    # here too, in case the orchestrator run is already dead (idempotent).
    if sprint.run_id:
        agent_runtime.request_cancel(sprint.run_id)
    for tid in current_ids:
        task = db.session.get(CodeDevTask, tid)
        if task and task.last_attempt_run_id:
            agent_runtime.request_cancel(task.last_attempt_run_id)
        dev_sprint_service.mark_cancelled(tid)
    return success_response({"sprint": sprint.to_dict()}, message="Sprint 已取消")


@dev_bp.route("/projects/<project_id>/dev-sessions/<session_id>/stop", methods=["POST"])
@jwt_required()
def stop_session(project_id: str, session_id: str):
    """Explicitly stop the dev session — tears down the container (credentials die with it)."""
    from datetime import datetime

    user_id = get_jwt_identity()
    if not _owned_project(project_id, user_id):
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)
    session = db.session.get(CodeDevSession, session_id)
    if not session or session.project_id != project_id or session.user_id != user_id:
        return error_response("NOT_FOUND", "开发会话不存在", 404)
    dev = _dev_service_for(session.lane)
    # Snapshot the work BEFORE tearing the container down — `stop_container` does
    # `docker rm -f`, which destroys the container fs (/work). Without this durable
    # snapshot, re-entering the session would fall back to the stale pre-dev-mode
    # source and appear to "rewrite the project from scratch".
    try:
        run_id = _latest_turn_run_id(project_id, user_id, session.lane)
        if run_id:
            files = dev.collect_source(project_id)
            if files:
                _snapshot_source_for(session.lane, run_id, project_id, files)
    except Exception:  # noqa: BLE001 — never block a stop on snapshotting
        pass
    # Frontend: also build + cache the production dist from the WARM container (its
    # node_modules is installed → a seconds-long vite build) before teardown, so a
    # later deploy serves this cache instead of a cold rebuild — near-instant deploy.
    if session.lane == "frontend":
        try:
            dist = dev.build_dist_in_container(project_id, base=f"/preview/{project_id}/")
            if dist:
                run_id = _latest_turn_run_id(project_id, user_id, "frontend")
                if run_id:
                    from backend.services.agent.workflows.code_dev_turn_workflow import (
                        persist_dist_cache_standalone,
                    )

                    persist_dist_cache_standalone(run_id, project_id, dist)
        except Exception:  # noqa: BLE001 — dist caching is best-effort
            pass
    try:
        dev.stop_container(project_id)
    except Exception:  # noqa: BLE001
        pass
    session.status = DevSessionStatus.STOPPED
    session.stopped_at = datetime.utcnow()
    db.session.commit()
    return success_response({"session": session.to_dict()}, message="开发会话已停止")


@dev_bp.route("/projects/<project_id>/dev-backend-sessions", methods=["POST"])
@jwt_required()
def start_backend_session(project_id: str):
    """Start (or resume) the interactive BACKEND dev session for a project.

    The backend-lane twin of ``start_session``: a long-running ``dev-be-<pid>``
    container serving the generated backend in hot-reload mode against an isolated
    dev database. The live frontend (frontend dev session) talks to it over
    ``/preview/<pid>/api``. When a prior backend project exists it is used directly
    and audited once; otherwise a minimal runnable backend is scaffolded."""
    drained = drain_guard()
    if drained:
        return drained
    user_id = get_jwt_identity()
    project = _owned_project(project_id, user_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)

    dev = get_dev_backend_service()
    if not dev.is_available():
        return error_response(
            "SERVER_ERROR", "后端开发模式不可用：未配置容器运行时或 Anthropic 凭证", 503
        )

    existing = _active_session(project_id, user_id, lane="backend")
    if existing:
        _reconcile_session(existing)
        # Reattach to the in-flight/last backend turn on refresh (see start_session).
        if existing.status in DevSessionStatus.ACTIVE:
            attach = _active_or_latest_turn_run_id(project_id, user_id, "backend")
            if attach:
                return success_response(
                    {**_session_view(existing, project_id, user_id), "run_id": attach}
                )
        if existing.status in DevSessionStatus.TERMINAL:
            existing.status = DevSessionStatus.STARTING
            db.session.commit()
        run = _start_dev_run(
            user_id, project.team_id, project_id,
            {"session_id": existing.id, "instruction": "", "bootstrap": True,
             "run_tests": False, "title": "后端开发会话恢复"},
            workflow="code_dev_backend_turn",
        )
        if run is None:
            return error_response("INSUFFICIENT_CREDITS", "积分不足", 402)
        return success_response({**_session_view(existing, project_id, user_id), "run_id": run.id})

    session = CodeDevSession(
        project_id=project_id,
        user_id=user_id,
        team_id=project.team_id,
        lane="backend",
        status=DevSessionStatus.STARTING,
        preview_path=f"/preview/{project_id}/api",
    )
    db.session.add(session)
    db.session.commit()

    ledger = load_dev_ledger(session, project)
    session.set_shared_ledger(ledger.to_dict())
    db.session.commit()
    seed_checklist(session.id, project_id, ledger.to_dict())

    # Prior backend project → use it directly + audit the checklist against it; none →
    # scaffold a minimal runnable backend and let the first turn build it per contract.
    try:
        prior = load_prior_source(project_id, "backend")
    except Exception:  # noqa: BLE001
        prior = {}
    if prior:
        boot_cfg = {
            "session_id": session.id, "instruction": "", "bootstrap": True,
            "audit": True, "run_tests": True, "title": "后端开发会话启动 · 校准功能清单",
        }
    else:
        boot_cfg = {
            "session_id": session.id, "bootstrap": True, "run_tests": False,
            "title": "后端开发会话启动",
            "instruction": (
                "按共享 API 契约初始化后端工程骨架:路由/数据模型/健康检查(/health)与关键文件桩,"
                "并写一个 dev-start.sh 用当前技术栈的热重载方式在 0.0.0.0:$PORT 启动;"
                "确保 dev server 能正常启动;本轮只搭骨架,功能将在后续对话回合逐步实现。"
            ),
        }
    run = _start_dev_run(
        user_id, project.team_id, project_id, boot_cfg, workflow="code_dev_backend_turn"
    )
    if run is None:
        db.session.delete(session)
        db.session.commit()
        return error_response("INSUFFICIENT_CREDITS", "积分不足", 402)
    return success_response(
        {**_session_view(session, project_id, user_id), "run_id": run.id}, message="后端开发会话已启动"
    )


@dev_bp.route("/projects/<project_id>/dev-sessions/<session_id>/run-tests", methods=["POST"])
@jwt_required()
def run_tests(project_id: str, session_id: str):
    """Run the contract-driven integration test against the session's dev backend.

    A test-only turn (no edit): the backend verify step runs the itest + house rules
    and folds the result onto the checklist. Only meaningful for a backend session."""
    drained = drain_guard()
    if drained:
        return drained
    user_id = get_jwt_identity()
    project = _owned_project(project_id, user_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)
    session = db.session.get(CodeDevSession, session_id)
    if not session or session.project_id != project_id or session.user_id != user_id:
        return error_response("NOT_FOUND", "开发会话不存在", 404)
    if session.lane != "backend":
        return error_response("VALIDATION_ERROR", "契约集成测试仅适用于后端开发会话", 400)
    if session.status in DevSessionStatus.TERMINAL:
        return error_response("VALIDATION_ERROR", "开发会话已停止，请重新启动后端开发模式", 400)

    run = _start_dev_run(
        user_id, project.team_id, project_id,
        {"session_id": session.id, "instruction": "", "run_tests": True, "title": "运行契约集成测试"},
        workflow="code_dev_backend_turn",
    )
    if run is None:
        return error_response("INSUFFICIENT_CREDITS", "积分不足", 402)
    return success_response({"run_id": run.id}, message="集成测试已开始")


@dev_bp.route("/preview-ws/authz", methods=["GET"])
def preview_ws_authz():
    """Internal nginx auth subrequest for the Dev Mode HMR websocket.

    Owner-only (design-review must-fix #6): the Vite dev server exposes source, so
    only the project owner (proven by the ``fe_preview_token`` cookie) may open the
    HMR socket. Returns 204 + ``X-App-Upstream: dev-<pid>:<port>`` for nginx to
    proxy to, or 403. Never JWT-gated (there's no header on a subrequest) — the
    cookie IS the credential, exactly like the deployed-app ws authz."""
    original_uri = request.headers.get("X-Original-URI", "")
    m = _PREVIEW_URI_RE.match(original_uri)
    if not m:
        return Response("", status=403)
    project_id = m.group(1)
    project = db.session.get(CodeProject, project_id)
    if not project:
        return Response("", status=403)
    token = request.cookies.get(_PREVIEW_COOKIE, "")
    identity = preview_identity(token, f"project:{project_id}")
    if not identity or identity != project.user_id:
        return Response("", status=403)
    # HMR is a frontend-lane concern (the Vite dev server); the backend dev container
    # has no HMR socket, so this always resolves the frontend session.
    session = (
        CodeDevSession.query.filter_by(project_id=project_id, user_id=project.user_id, lane="frontend")
        .filter(CodeDevSession.status.in_([DevSessionStatus.RUNNING, DevSessionStatus.REPAIRING]))
        .order_by(CodeDevSession.created_at.desc())
        .first()
    )
    if not session or not session.container_name:
        return Response("", status=403)
    resp = Response("", status=204)
    resp.headers["X-App-Upstream"] = f"{session.container_name}:{session.internal_port or 5173}"
    return resp
