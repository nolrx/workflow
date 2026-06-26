"""
Agent Swarm API — shared across product domains.

Endpoints (registered at /api/agent):
    POST   /runs                       start a workflow run (reserves credits)
    GET    /runs/<run_id>              full snapshot (run + steps + events + artifacts)
    GET    /runs/<run_id>/stream       live SSE event stream (replay + push)
    POST   /runs/<run_id>/cancel       request cooperative cancellation
    POST   /runs/<run_id>/retry        relaunch a failed run to retry its failed stage
    GET    /artifacts/<id>/file        download / view a produced artifact
"""

import json
import logging
import os
import queue

from flask import (
    Blueprint,
    Response,
    current_app,
    request,
    send_file,
    send_from_directory,
    stream_with_context,
)
from flask_jwt_extended import get_jwt_identity, jwt_required

from backend.extensions import db
from backend.models.agent import (
    AgentArtifact,
    AgentEvent,
    AgentEventType,
    AgentRun,
    AgentRunStatus,
)
from backend.services import pricing
from backend.services.agent.bus import event_bus
from backend.services.agent.files import artifact_abs_path
from backend.services.agent.runtime import agent_runtime, get_workflow
from backend.services.credit_service import InsufficientCreditsError, deduct_credits
from backend.services.lifecycle import drain_guard
from backend.utils.preview_token import PREVIEW_TOKEN_TTL, mint_preview_token, preview_identity
from backend.utils.response import error_response, success_response

logger = logging.getLogger(__name__)

agent_bp = Blueprint("agent", __name__)

# Estimated credit cost per workflow (reserved up-front; auto-refunded on early
# failure — see agent runtime). Values come from the central pricing table.
WORKFLOW_COSTS = {
    "code_full_generation": pricing.CODE_FULL_GENERATION_TOTAL,
    "code_frontend_project_generation": pricing.CODE_FRONTEND_PROJECT_GENERATION,
    "code_canvas_generation": pricing.CODE_CANVAS_RUN,
    "code_figma_slice_generation": pricing.CODE_FIGMA_SLICE_TOTAL,
    # Full-stack pipeline (concurrent frontend + backend + middleware + deploy).
    "code_backend_project_generation": pricing.CODE_BACKEND_PROJECT_GENERATION,
    "code_middleware_provisioning": pricing.CODE_MIDDLEWARE_PROVISIONING,
    "code_fullstack_deploy": pricing.CODE_FULLSTACK_DEPLOY,
    # Secondary development: impact analysis + execution plan for a deployed app.
    "code_app_iteration_analysis": pricing.CODE_APP_ITERATION_ANALYSIS,
}
VALID_DOMAINS = {"code"}
# The full-stack pipeline starts THREE concurrent runs per project (frontend +
# backend + middleware), so the per-user active-run cap must clear 3 with
# headroom. Env-tunable for ops. Default doubled 6 -> 12 to let users run more
# concurrent generations (kept in step with AGENT_MAX_WORKERS).
MAX_CONCURRENT_RUNS = int(os.getenv("AGENT_MAX_CONCURRENT_RUNS", "12"))


def _get_owned_run(run_id: str) -> AgentRun | None:
    user_id = get_jwt_identity()
    return AgentRun.query.filter_by(id=run_id, user_id=user_id).first()


@agent_bp.route("/runs", methods=["POST"])
@jwt_required()
def create_run():
    """Create and start a workflow run after reserving credits."""
    # Refuse new work while the platform is draining for a redeploy (in-flight runs
    # keep going and resume on the new process; users just retry once it's back).
    drained = drain_guard()
    if drained:
        return drained
    user_id = get_jwt_identity()
    data = request.get_json() or {}
    domain = (data.get("domain") or "").strip()
    workflow = (data.get("workflow") or "").strip()
    resource_type = data.get("resource_type")
    resource_id = data.get("resource_id")
    config = data.get("config") or {}
    team_id = data.get("team_id")

    if domain not in VALID_DOMAINS:
        return error_response("VALIDATION_ERROR", "缺少有效的 domain", 400)
    if workflow not in WORKFLOW_COSTS or get_workflow(workflow) is None:
        return error_response("VALIDATION_ERROR", f"不支持的 workflow: {workflow}", 400)
    if not isinstance(config, dict):
        return error_response("VALIDATION_ERROR", "config 必须是对象", 400)

    # Workflow-specific minimal input validation.
    if workflow == "code_frontend_project_generation" and not resource_id:
        return error_response(
            "VALIDATION_ERROR", "前端生成需要一个已确认的 Code 项目（resource_id）", 400
        )
    if workflow == "code_figma_slice_generation" and not resource_id:
        return error_response(
            "VALIDATION_ERROR", "切片导出需要一个已有的 Code 项目（resource_id）", 400
        )
    if (
        workflow
        in (
            "code_backend_project_generation",
            "code_middleware_provisioning",
            "code_fullstack_deploy",
        )
        and not resource_id
    ):
        return error_response(
            "VALIDATION_ERROR", "全栈生成/部署需要一个已有的 Code 项目（resource_id）", 400
        )
    if workflow == "code_full_generation":
        has_requirement = bool((config.get("requirement") or "").strip())
        if not has_requirement and not resource_id:
            return error_response(
                "VALIDATION_ERROR", "请提供 requirement 或已有的 resource_id", 400
            )
    if workflow == "code_canvas_generation":
        if not resource_id:
            return error_response(
                "VALIDATION_ERROR", "画布执行需要一个 Code 项目（resource_id）", 400
            )
        if not (config.get("canvas_id") or "").strip():
            return error_response("VALIDATION_ERROR", "请提供要执行的画布（canvas_id）", 400)

    # Per-user concurrency cap on active runs.
    active = AgentRun.query.filter(
        AgentRun.user_id == user_id,
        AgentRun.status.in_(list(AgentRunStatus.ACTIVE)),
    ).count()
    if active >= MAX_CONCURRENT_RUNS:
        return error_response("CONCURRENCY_LIMIT", f"已有 {active} 个进行中的任务，请稍后再试", 429)

    cost = WORKFLOW_COSTS[workflow]
    title = (config.get("title") or "").strip() or None

    run = AgentRun(
        user_id=user_id,
        team_id=team_id,
        domain=domain,
        workflow=workflow,
        resource_type=resource_type,
        resource_id=resource_id,
        title=title,
        status=AgentRunStatus.QUEUED,
        credit_reserved=cost,
    )
    run.set_config(config)
    run.set_input_snapshot(
        {
            "domain": domain,
            "workflow": workflow,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "config": config,
        }
    )
    db.session.add(run)
    db.session.commit()

    # Reserve credits up-front (this is the first real caller of deduct_credits).
    # When the workflow is priced free (cost <= 0, e.g. Code-domain metering is
    # disabled) skip the deduction entirely — deduct_credits rejects a zero/negative
    # amount, and there is nothing to reserve or block on.
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
            return error_response("INSUFFICIENT_CREDITS", "积分不足，无法启动任务", 402)
        except Exception as exc:  # noqa: BLE001
            logger.error("Credit reservation failed for run: %s", exc, exc_info=True)
            db.session.delete(run)
            db.session.commit()
            return error_response("SERVER_ERROR", "扣费失败，任务未启动", 500)

    agent_runtime.start(current_app._get_current_object(), run.id)

    return success_response(
        {
            "run_id": run.id,
            "status": run.status,
            "stream_url": f"/api/agent/runs/{run.id}/stream",
        },
        "任务已启动",
        201,
    )


@agent_bp.route("/runs/<run_id>", methods=["GET"])
@jwt_required()
def get_run(run_id: str):
    """Return the full run snapshot — used for initial load and reconnect."""
    run = _get_owned_run(run_id)
    if not run:
        return error_response("NOT_FOUND", "任务不存在", 404)
    return success_response({"run": run.to_dict(include_children=True)})


@agent_bp.route("/runs", methods=["GET"])
@jwt_required()
def list_runs():
    """List the current user's recent runs (compact)."""
    user_id = get_jwt_identity()
    limit = min(int(request.args.get("limit", 20)), 100)
    domain = request.args.get("domain")
    resource_id = request.args.get("resource_id")
    workflow = request.args.get("workflow")
    query = AgentRun.query.filter_by(user_id=user_id)
    if domain:
        query = query.filter_by(domain=domain)
    if resource_id:
        # Used to find (and replay) the run(s) tied to a given project/task.
        query = query.filter_by(resource_id=resource_id)
    if workflow:
        # A resource accumulates runs across several workflows (e.g. a Code
        # project has its conversation run plus auxiliary frontend / figma /
        # canvas runs). Replay targets one specific workflow, so allow scoping
        # to it instead of blindly taking the latest run of any kind.
        query = query.filter_by(workflow=workflow)
    runs = query.order_by(AgentRun.created_at.desc()).limit(limit).all()
    return success_response({"runs": [run.to_dict() for run in runs]})


@agent_bp.route("/runs/<run_id>/cancel", methods=["POST"])
@jwt_required()
def cancel_run(run_id: str):
    """Request cooperative cancellation; remaining steps stop, in-flight model calls are not killed."""
    run = _get_owned_run(run_id)
    if not run:
        return error_response("NOT_FOUND", "任务不存在", 404)
    if run.status in AgentRunStatus.TERMINAL:
        return error_response("INVALID_STATE", "任务已结束，无法取消", 400)
    agent_runtime.request_cancel(run_id)
    return success_response({"run_id": run_id, "status": "cancelling"}, "已请求取消")


@agent_bp.route("/runs/<run_id>/resume", methods=["POST"])
@jwt_required()
def resume_run(run_id: str):
    """Resume a paused run after a human-in-the-loop review checkpoint.

    Body: ``{"action": "approve"|"revise", "stage": <review_stage>, "instruction": str}``

    - ``approve`` advances past the reviewed document into the next stage(s).
    - ``revise`` regenerates the reviewed document from the user's instruction
      (which is also folded into the context ledger), then pauses again.

    The directive is stashed on the run config; the worker is relaunched and the
    workflow rebuilds its state from the persisted cursor / ledger / project.
    """
    drained = drain_guard()  # relaunching a worker counts as new work
    if drained:
        return drained
    run = _get_owned_run(run_id)
    if not run:
        return error_response("NOT_FOUND", "任务不存在", 404)
    if run.status != AgentRunStatus.PAUSED:
        return error_response("INVALID_STATE", "任务当前不在等待确认状态", 400)

    data = request.get_json() or {}
    action = (data.get("action") or "").strip()
    if action not in ("approve", "revise", "select_style"):
        return error_response(
            "VALIDATION_ERROR", "action 必须为 approve、revise 或 select_style", 400
        )

    progress = run.get_progress()
    review_stage = progress.get("review_stage")
    stage = (data.get("stage") or review_stage or "").strip()
    if review_stage and stage != review_stage:
        return error_response("VALIDATION_ERROR", "确认环节与当前等待的步骤不一致", 400)

    instruction = (data.get("instruction") or "").strip()
    if action == "revise" and not instruction:
        return error_response("VALIDATION_ERROR", "请填写调整意见", 400)

    # select_style carries the user's UI style picks; at least one is required.
    style_ids = data.get("style_ids") or []
    if action == "select_style" and (not isinstance(style_ids, list) or not style_ids):
        return error_response("VALIDATION_ERROR", "请至少选择一个 UI 风格", 400)

    # Stash the resume directive for the workflow, mark RUNNING to block a
    # duplicate resume racing in, clear the review flag, then relaunch the worker.
    config = run.get_config()
    config["_resume"] = {
        "action": action,
        "stage": stage,
        "instruction": instruction,
        "style_ids": style_ids,
    }
    run.set_config(config)
    run.status = AgentRunStatus.RUNNING
    progress["review_stage"] = None
    run.set_progress(progress)
    db.session.commit()

    agent_runtime.start(current_app._get_current_object(), run_id)
    resume_message = {
        "approve": "已确认，继续生成",
        "revise": "已收到调整意见，正在重新生成",
        "select_style": "已选择风格，正在生成风格文档",
    }.get(action, "已继续生成")
    return success_response(
        {
            "run_id": run_id,
            "status": run.status,
            "stream_url": f"/api/agent/runs/{run_id}/stream",
        },
        resume_message,
    )


# Terminal statuses from which a run may be relaunched to retry its failed stage.
# A hard FAILED run stopped at the failing stage; a PARTIAL run completed but left
# one stage (e.g. preview image generation) failed. Both can resume the remaining
# work without restarting from scratch.
_RETRYABLE_STATUSES = {AgentRunStatus.FAILED, AgentRunStatus.PARTIAL}


@agent_bp.route("/runs/<run_id>/retry", methods=["POST"])
@jwt_required()
def retry_run(run_id: str):
    """Relaunch a failed run to retry its failed stage (re-using completed stages).

    Body (optional): ``{"stage": <agent_key of the failed step>}`` — a precise hint
    for which stage to resume from. The workflow is authoritative: it re-derives the
    failed stage from the persisted step that ended ``failed`` and continues from
    there, re-using everything earlier stages already produced (documents, ledger,
    project). The worker keeps its original ``started_at`` so the runtime treats
    this as a continuation, not a fresh run.
    """
    drained = drain_guard()  # relaunching a worker counts as new work
    if drained:
        return drained
    user_id = get_jwt_identity()
    run = _get_owned_run(run_id)
    if not run:
        return error_response("NOT_FOUND", "任务不存在", 404)
    if run.status not in _RETRYABLE_STATUSES:
        return error_response("INVALID_STATE", "仅失败或部分完成的任务可以重试当前阶段", 400)

    # Per-user concurrency cap (the run is terminal, so it is not counted as active).
    active = AgentRun.query.filter(
        AgentRun.user_id == user_id,
        AgentRun.status.in_(list(AgentRunStatus.ACTIVE)),
    ).count()
    if active >= MAX_CONCURRENT_RUNS:
        return error_response("CONCURRENCY_LIMIT", f"已有 {active} 个进行中的任务，请稍后再试", 429)

    data = request.get_json() or {}
    stage = (data.get("stage") or "").strip() or None

    # Re-reserve credits only when the original failure refunded them (early failure
    # with no artifacts → credit_used == 0). A run that already kept its charge
    # (produced documents/images) retries without paying twice.
    if run.credit_reserved and run.credit_used == 0:
        try:
            deduct_credits(
                user_id=user_id,
                amount=run.credit_reserved,
                operation="agent_run",
                resource_type="agent_run",
                resource_id=run.id,
                description=f"Agent run retry: {run.workflow}",
                team_id=run.team_id,
            )
        except InsufficientCreditsError:
            return error_response("INSUFFICIENT_CREDITS", "积分不足，无法重试", 402)
        except Exception as exc:  # noqa: BLE001
            logger.error("Credit reservation failed for retry: %s", exc, exc_info=True)
            return error_response("SERVER_ERROR", "扣费失败，未能重试", 500)

    # Stash the one-shot retry directive, flip the run back to RUNNING (so a
    # duplicate retry racing in is rejected), clear the terminal markers, relaunch.
    config = run.get_config()
    config["_resume"] = {"action": "retry", "stage": stage}
    run.set_config(config)
    run.status = AgentRunStatus.RUNNING
    run.error_message = None
    run.completed_at = None
    db.session.commit()

    agent_runtime.start(current_app._get_current_object(), run_id)
    return success_response(
        {
            "run_id": run_id,
            "status": run.status,
            "stream_url": f"/api/agent/runs/{run_id}/stream",
        },
        "正在重试当前阶段",
    )


def _is_stream_end(run) -> bool:
    """A stream segment ends at a terminal status OR a pause (awaiting the user).

    A paused run's worker has exited, so no more live events will arrive until the
    user resumes — which opens a brand-new stream. Ending the SSE here (with a
    ``done`` event) lets the client cleanly settle into the awaiting-review UI
    instead of hanging on keepalives.
    """
    return bool(
        run and (run.status in AgentRunStatus.TERMINAL or run.status == AgentRunStatus.PAUSED)
    )


def _sse(event_dict: dict) -> str:
    return (
        f"id: {event_dict['sequence']}\n"
        f"event: agent_event\n"
        f"data: {json.dumps(event_dict, ensure_ascii=False)}\n\n"
    )


def _sse_delta(event_dict: dict) -> str:
    return f"event: agent_delta\ndata: {json.dumps(event_dict, ensure_ascii=False)}\n\n"


def _event_stream(app, run_id: str, last_sequence: int):
    """SSE generator: subscribe first, replay missed events, then push live."""
    q = event_bus.subscribe(run_id)
    cursor = last_sequence
    terminal = False
    try:
        # Initial replay from the DB (source of truth) for anything already logged.
        with app.app_context():
            events = (
                AgentEvent.query.filter(AgentEvent.run_id == run_id, AgentEvent.sequence > cursor)
                .order_by(AgentEvent.sequence)
                .all()
            )
            payloads = [event.to_dict() for event in events]
            run = db.session.get(AgentRun, run_id)
            terminal = _is_stream_end(run)
        for payload in payloads:
            cursor = max(cursor, payload["sequence"])
            yield _sse(payload)

        while True:
            if terminal:
                # Final sweep for events that landed between replay and now.
                with app.app_context():
                    late = (
                        AgentEvent.query.filter(
                            AgentEvent.run_id == run_id, AgentEvent.sequence > cursor
                        )
                        .order_by(AgentEvent.sequence)
                        .all()
                    )
                    late_payloads = [event.to_dict() for event in late]
                for payload in late_payloads:
                    cursor = max(cursor, payload["sequence"])
                    yield _sse(payload)
                yield "event: done\ndata: {}\n\n"
                break

            try:
                event = q.get(timeout=15)
            except queue.Empty:
                yield ": keepalive\n\n"
                with app.app_context():
                    run = db.session.get(AgentRun, run_id)
                    terminal = _is_stream_end(run)
                continue

            # Transient token deltas are live-only: forward immediately without
            # touching the sequence cursor (they are never persisted or replayed).
            if event.get("kind") == "delta":
                yield _sse_delta(event)
                continue

            if event["sequence"] <= cursor:
                continue
            cursor = max(cursor, event["sequence"])
            yield _sse(event)
            # run_completed ends the stream; so does a pause checkpoint
            # (step_awaiting_review) — the worker has exited and won't push again
            # until the user resumes, which opens a fresh stream.
            if event["event_type"] in (
                AgentEventType.RUN_COMPLETED,
                AgentEventType.STEP_AWAITING_REVIEW,
            ):
                terminal = True
    finally:
        event_bus.unsubscribe(run_id, q)


@agent_bp.route("/runs/<run_id>/stream", methods=["GET"])
@jwt_required()
def stream_run(run_id: str):
    """Stream run events as text/event-stream (consumed via fetch + ReadableStream)."""
    run = _get_owned_run(run_id)
    if not run:
        return error_response("NOT_FOUND", "任务不存在", 404)
    try:
        last_sequence = int(request.args.get("last_sequence", 0))
    except (TypeError, ValueError):
        last_sequence = 0

    app = current_app._get_current_object()
    response = Response(
        stream_with_context(_event_stream(app, run_id, last_sequence)),
        mimetype="text/event-stream",
    )
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@agent_bp.route("/artifacts/<artifact_id>/file", methods=["GET"])
@jwt_required()
def get_artifact_file(artifact_id: str):
    """Serve a produced artifact (on-disk file or inline text), owner-only."""
    user_id = get_jwt_identity()
    artifact = db.session.get(AgentArtifact, artifact_id)
    if not artifact:
        return error_response("NOT_FOUND", "产物不存在", 404)
    run = db.session.get(AgentRun, artifact.run_id)
    if not run or run.user_id != user_id:
        return error_response("NOT_FOUND", "产物不存在", 404)

    as_attachment = bool(request.args.get("download"))

    if artifact.storage_path:
        abs_path = artifact_abs_path(artifact.storage_path)
        if not os.path.exists(abs_path):
            return error_response("NOT_FOUND", "文件已不存在", 404)
        return send_file(
            abs_path,
            mimetype=artifact.mime_type or "application/octet-stream",
            as_attachment=as_attachment,
            download_name=artifact.filename or f"{artifact.id}.bin",
        )

    if artifact.content_text is not None:
        return Response(
            artifact.content_text,
            mimetype=artifact.mime_type or "text/plain; charset=utf-8",
        )

    return error_response("NOT_FOUND", "该产物没有可下载的文件内容", 404)


# Cookie that re-authenticates the dist's relative asset requests (which carry no
# query string) after the entry ``index.html?token=...`` request.
_PREVIEW_COOKIE = "fe_preview_token"
# Defense-in-depth for the same-origin sandboxed preview: block fetch/XHR/WebSocket
# egress (the agent-generated app needs no network) while still allowing the
# same-origin assets + inline modulepreload a normal Vite build emits.
# _PREVIEW_CSP = "default-src 'self' 'unsafe-inline' data: blob:; connect-src 'none'"
_PREVIEW_CSP = ""  # 停用安全策略


@agent_bp.route("/runs/<run_id>/site/<path:filename>", methods=["GET"])
def serve_run_site(run_id: str, filename: str):
    """Serve a file from a run's built dist for iframe preview.

    iframes cannot send an Authorization header, so ownership is verified via a
    ``?token=`` query JWT on the entry request; that token is mirrored into a
    short-lived, path-scoped cookie so the dist's relative asset requests stay
    authenticated. The dist is built with ``base: './'`` so its assets resolve
    relative to this route's path.
    """
    token = request.args.get("token", "") or request.cookies.get(_PREVIEW_COOKIE, "")
    # Accepts the one-shot ?token= access token (entry) or a minted, run-scoped
    # preview token (cookie) that outlives the 30-min access token.
    identity = preview_identity(token, f"run:{run_id}")
    if not identity:
        return error_response("FORBIDDEN", "无效的预览令牌", 403)
    run = AgentRun.query.filter_by(id=run_id, user_id=identity).first()
    if not run:
        return error_response("NOT_FOUND", "任务不存在", 404)
    site_dir = artifact_abs_path(f"agent_runs/{run_id}/site")
    if not os.path.isdir(site_dir):
        return error_response("NOT_FOUND", "预览不存在或尚未生成", 404)

    response = send_from_directory(site_dir, filename)
    response.headers["Content-Security-Policy"] = _PREVIEW_CSP
    # On the entry request (token in the query), exchange the one-shot access token
    # for a longer-lived run-scoped preview token and pin it to this run's site path,
    # so subsequent relative asset fetches authenticate via the cookie — and keep
    # working past the 30-min access-token expiry.
    if request.args.get("token"):
        site_root = request.path[: request.path.find("/site/") + len("/site/")]
        response.set_cookie(
            _PREVIEW_COOKIE,
            mint_preview_token(identity, f"run:{run_id}"),
            max_age=PREVIEW_TOKEN_TTL,
            httponly=True,
            samesite="Lax",
            path=site_root,
        )
    return response
