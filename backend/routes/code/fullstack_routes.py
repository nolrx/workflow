"""
Full-stack generation orchestration routes (mounted at /api/code).

This is the single backend entrypoint the frontend calls to drive the
frontend + backend + middleware pipeline:

    POST /projects/<pid>/fullstack/runs   synthesize the shared contract, then
                                          start the THREE concurrent runs
    POST /projects/<pid>/deploy           start the atomic deploy run
    GET  /projects/<pid>/fullstack/status three runs + deployment snapshot
    GET  /projects/<pid>/contract         the shared OpenAPI contract
    ANY  /app/<pid>/api/<path>            reverse proxy to the live backend container

Creating all three runs server-side guarantees the shared OpenAPI contract is
frozen before any run starts, so the concurrent runs never race on it. The
reverse proxy is how the served frontend (``/preview/<pid>/``) reaches the
generated backend for real — auth rides a path-scoped cookie set by the preview
entry, mirroring the existing static preview.
"""
import logging
import threading

import requests
from flask import Blueprint, Response, current_app, request, stream_with_context
from flask_jwt_extended import get_jwt_identity, jwt_required

from backend.extensions import db
from backend.models.agent import AgentRun, AgentRunStatus
from backend.models.code import CodeProject
from backend.models.code.fullstack import CodeDeployment, ContractStatus
from backend.services import pricing
from backend.services.agent.runtime import agent_runtime
from backend.services.code import deploy_service
from backend.services.code.fullstack import contract_service
from backend.services.credit_service import InsufficientCreditsError, deduct_credits
from backend.services.lifecycle import drain_guard
from backend.utils.preview_token import preview_identity
from backend.utils.response import error_response, success_response

logger = logging.getLogger(__name__)

fullstack_bp = Blueprint("code_fullstack", __name__)
# Mounted top-level at /app (like /preview) so the served frontend's same-origin
# API calls (/app/<pid>/api/...) reach the live backend container.
app_proxy_bp = Blueprint("code_app_proxy", __name__)

# The three concurrent generation workflows + their reservation costs.
_PIPELINE = {
    "frontend": ("code_frontend_project_generation", pricing.CODE_FRONTEND_PROJECT_GENERATION),
    "backend": ("code_backend_project_generation", pricing.CODE_BACKEND_PROJECT_GENERATION),
    "middleware": ("code_middleware_provisioning", pricing.CODE_MIDDLEWARE_PROVISIONING),
}
_PIPELINE_WORKFLOWS = {wf for wf, _ in _PIPELINE.values()}

# Serialize the "is a trio already in flight? -> if not, create one" critical
# section. Without it a double-submit (two concurrent requests from a fast
# double-click) both read no active runs before either commits, and each creates
# a full trio — duplicating three runs + their containers. The lock is held only
# around the cheap check+create (NOT the slow contract synthesis), so distinct
# projects barely contend; the single gunicorn worker means one lock suffices.
_trio_creation_lock = threading.Lock()

# Hop-by-hop headers never forwarded by the reverse proxy.
_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}
# Cookie that authenticates the served frontend's calls to /app/<pid>/api.
APP_TOKEN_COOKIE = "fs_app_token"


def _owned_project(project_id: str, user_id: str) -> CodeProject | None:
    return CodeProject.query.filter_by(id=project_id, user_id=user_id).first()


def _start_run(user_id, team_id, workflow: str, project_id: str, config: dict) -> AgentRun | None:
    """Reserve credits and start one run (mirrors agent_routes.create_run core).

    Returns the started run, or ``None`` on insufficient credits (caller decides
    how to surface it). Bypasses the per-user concurrency cap on purpose — the
    trio is created atomically by the server, not by user spam.
    """
    cost = _cost_for(workflow)
    run = AgentRun(
        user_id=user_id, team_id=team_id, domain="code", workflow=workflow,
        resource_type="code_project", resource_id=project_id,
        title=config.get("title"), status=AgentRunStatus.QUEUED, credit_reserved=cost,
    )
    run.set_config(config)
    run.set_input_snapshot({
        "domain": "code", "workflow": workflow,
        "resource_type": "code_project", "resource_id": project_id, "config": config,
    })
    db.session.add(run)
    db.session.commit()
    if cost > 0:
        try:
            deduct_credits(
                user_id=user_id, amount=cost, operation="agent_run",
                resource_type="agent_run", resource_id=run.id,
                description=f"Agent run: {workflow}", team_id=team_id,
            )
        except InsufficientCreditsError:
            db.session.delete(run)
            db.session.commit()
            return None
    agent_runtime.start(current_app._get_current_object(), run.id)
    return run


def _cost_for(workflow: str) -> int:
    return {
        "code_frontend_project_generation": pricing.CODE_FRONTEND_PROJECT_GENERATION,
        "code_backend_project_generation": pricing.CODE_BACKEND_PROJECT_GENERATION,
        "code_middleware_provisioning": pricing.CODE_MIDDLEWARE_PROVISIONING,
        "code_fullstack_deploy": pricing.CODE_FULLSTACK_DEPLOY,
    }.get(workflow, 0)


def _active_pipeline_runs(project_id: str, user_id: str) -> dict:
    """Map workflow -> active run for any in-flight pipeline run on this project."""
    rows = AgentRun.query.filter(
        AgentRun.resource_id == project_id,
        AgentRun.user_id == user_id,
        AgentRun.workflow.in_(list(_PIPELINE_WORKFLOWS)),
        AgentRun.status.in_(list(AgentRunStatus.ACTIVE)),
    ).all()
    return {r.workflow: r for r in rows}


@fullstack_bp.route("/projects/<project_id>/fullstack/runs", methods=["POST"])
@jwt_required()
def start_fullstack(project_id: str):
    """Synthesize the shared contract, then start the three concurrent runs."""
    drained = drain_guard()  # starts three background runs — refuse while draining
    if drained:
        return drained
    user_id = get_jwt_identity()
    project = _owned_project(project_id, user_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)
    if not project.requirements_doc or not project.development_flow:
        return error_response(
            "VALIDATION_ERROR", "项目尚未完成需求文档与开发流程，无法生成全栈应用", 400
        )

    team_id = project.team_id

    # 1) Freeze the shared OpenAPI contract BEFORE any run starts.
    try:
        ledger = contract_service.ensure_contract(project, user_id, team_id)
    except Exception as error:  # noqa: BLE001
        logger.error("contract synthesis failed: %s", error, exc_info=True)
        return error_response("SERVER_ERROR", f"共享 API 契约合成失败：{error}", 500)
    if ledger.contract_status != ContractStatus.READY:
        return error_response("SERVER_ERROR", "共享 API 契约未就绪", 500)

    # 2) Start the three concurrent runs (reuse any already in flight). Read the
    #    active set and create under the lock so two concurrent submits can't each
    #    pass an empty check and duplicate the trio — the re-read here is the half
    #    that has to be inside the critical section.
    runs: dict[str, str] = {}
    with _trio_creation_lock:
        active = _active_pipeline_runs(project_id, user_id)
        for key, (workflow, _cost) in _PIPELINE.items():
            existing = active.get(workflow)
            if existing:
                runs[key] = existing.id
                continue
            run = _start_run(user_id, team_id, workflow, project_id, {"pipeline": key})
            if run is None:
                return error_response("INSUFFICIENT_CREDITS", "积分不足，无法启动全栈生成", 402)
            runs[key] = run.id

    return success_response(
        {
            "runs": runs,
            "contract": ledger.to_dict(),
            "stream_urls": {k: f"/api/agent/runs/{v}/stream" for k, v in runs.items()},
        },
        "全栈生成已启动（前端 / 后端 / 中间件并发）",
        201,
    )


@fullstack_bp.route("/projects/<project_id>/deploy", methods=["POST"])
@jwt_required()
def start_deploy(project_id: str):
    """Start the atomic deploy run (requires the three generation runs done)."""
    drained = drain_guard()  # starts a background deploy run — refuse while draining
    if drained:
        return drained
    user_id = get_jwt_identity()
    project = _owned_project(project_id, user_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)

    # Require a completed backend run (the deploy reads its source).
    backend_done = (
        AgentRun.query.filter_by(
            resource_id=project_id, user_id=user_id,
            workflow="code_backend_project_generation",
        )
        .filter(AgentRun.status.in_(list(AgentRunStatus.TERMINAL)))
        .filter(AgentRun.status.in_([AgentRunStatus.COMPLETED, AgentRunStatus.PARTIAL]))
        .first()
    )
    if not backend_done:
        return error_response("VALIDATION_ERROR", "后端工程尚未生成完成，无法部署", 400)

    # Require a completed frontend run too — without a built frontend there is
    # nothing to serve at /preview/<pid>/ for the deployed backend to talk to, so
    # a backend-only "successful" deploy would leave a blank preview. Mirrors the
    # client-side deployReady() gate and the design's "all three runs done first".
    # Middleware stays OPTIONAL on purpose: deploy provisions a namespace itself
    # and degrades gracefully (best-effort init.sql) when no middleware run exists.
    frontend_done = (
        AgentRun.query.filter_by(
            resource_id=project_id, user_id=user_id,
            workflow="code_frontend_project_generation",
        )
        .filter(AgentRun.status.in_([AgentRunStatus.COMPLETED, AgentRunStatus.PARTIAL]))
        .first()
    )
    if not frontend_done:
        return error_response("VALIDATION_ERROR", "前端工程尚未生成完成，无法部署", 400)

    # Avoid duplicate deploy runs.
    in_flight = AgentRun.query.filter(
        AgentRun.resource_id == project_id, AgentRun.user_id == user_id,
        AgentRun.workflow == "code_fullstack_deploy",
        AgentRun.status.in_(list(AgentRunStatus.ACTIVE)),
    ).first()
    if in_flight:
        return success_response(
            {"run_id": in_flight.id, "stream_url": f"/api/agent/runs/{in_flight.id}/stream"},
            "部署已在进行中",
        )

    run = _start_run(user_id, project.team_id, "code_fullstack_deploy", project_id, {})
    if run is None:
        return error_response("INSUFFICIENT_CREDITS", "积分不足，无法启动部署", 402)
    return success_response(
        {"run_id": run.id, "stream_url": f"/api/agent/runs/{run.id}/stream"},
        "原子部署已启动",
        201,
    )


@fullstack_bp.route("/projects/<project_id>/fullstack/status", methods=["GET"])
@jwt_required()
def fullstack_status(project_id: str):
    """A snapshot of the three pipeline runs + the deployment for this project."""
    user_id = get_jwt_identity()
    project = _owned_project(project_id, user_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)

    def latest(workflow: str) -> dict | None:
        run = (
            AgentRun.query.filter_by(resource_id=project_id, user_id=user_id, workflow=workflow)
            .order_by(AgentRun.created_at.desc())
            .first()
        )
        return run.to_dict() if run else None

    deployment = CodeDeployment.query.filter_by(project_id=project_id).first()
    ledger = contract_service.get_ledger(project_id)
    return success_response({
        "runs": {
            "frontend": latest("code_frontend_project_generation"),
            "backend": latest("code_backend_project_generation"),
            "middleware": latest("code_middleware_provisioning"),
            "deploy": latest("code_fullstack_deploy"),
        },
        "deployment": deployment.to_dict() if deployment else None,
        "contract_status": ledger.contract_status if ledger else "pending",
    })


@fullstack_bp.route("/projects/<project_id>/contract", methods=["GET"])
@jwt_required()
def get_contract(project_id: str):
    """Return the synthesized shared API contract + middleware manifest."""
    user_id = get_jwt_identity()
    project = _owned_project(project_id, user_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)
    ledger = contract_service.get_ledger(project_id)
    if not ledger:
        return error_response("NOT_FOUND", "尚未合成共享 API 契约", 404)
    return success_response({"contract": ledger.to_dict()})


# --- Reverse proxy: served frontend -> live backend container ----------------
def _proxy_identity(project_id: str) -> str | None:
    """Owner identity from the app-token cookie or a one-shot ?token= query.

    The cookie holds a project-scoped preview token (minted by the preview entry);
    it is verified against THIS project so a token minted for another project's
    preview cannot reach this backend.
    """
    token = request.args.get("token", "") or request.cookies.get(APP_TOKEN_COOKIE, "")
    return preview_identity(token, f"project:{project_id}")


@app_proxy_bp.route(
    "/<project_id>/api/", defaults={"subpath": ""},
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
@app_proxy_bp.route(
    "/<project_id>/api/<path:subpath>",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
def proxy_to_backend(project_id: str, subpath: str):
    """Forward a request from the served frontend to the live backend container.

    The generated backend mounts its routes at root, so ``/app/<pid>/api/<sub>``
    maps to ``http://<container>:<port>/<sub>``.

    Access mirrors ``serve_project_preview``: the OWNER (proven by the app-token
    cookie / one-shot ``?token=``) OR anyone when the project is public. A public
    project's served frontend (``/preview/<pid>/``) is reachable anonymously, so its
    same-origin API calls MUST be too — otherwise a shared app (e.g. a link opened
    in WeChat / another browser that never ran the cookie-planting ``?token=``
    entry) renders its shell but every ``/app/<pid>/api/*`` call 403s. The
    deployment is always resolved against the project OWNER (an anonymous public
    visitor has no identity of its own); the generated backend keeps its OWN auth,
    so exposing the proxy for a public project grants nothing the public preview
    didn't already expose.
    """
    project = CodeProject.query.filter_by(id=project_id).first()
    if not project:
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)
    identity = _proxy_identity(project_id)
    is_owner = bool(identity) and identity == project.user_id
    is_public = project.visibility == "public"
    if not (is_owner or is_public):
        return error_response("FORBIDDEN", "无效的访问令牌", 403)
    target = deploy_service.resolve_proxy_target(project_id, project.user_id)
    if not target:
        return error_response("NOT_FOUND", "后端尚未部署或未在运行", 404)
    container, port = target

    upstream = f"http://{container}:{port}/{subpath}"
    fwd_headers = {
        k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS
    }
    try:
        resp = requests.request(
            method=request.method,
            url=upstream,
            params=request.args.to_dict(flat=False),
            headers=fwd_headers,
            data=request.get_data(),
            stream=True,
            timeout=(5, 120),
            allow_redirects=False,
        )
    except requests.RequestException as error:
        logger.warning("proxy to %s failed: %s", upstream, error)
        return error_response("SERVER_ERROR", f"后端不可达：{error}", 502)

    excluded = _HOP_HEADERS | {"content-encoding", "content-length"}
    headers = [(k, v) for k, v in resp.headers.items() if k.lower() not in excluded]

    def generate():
        try:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
        finally:
            resp.close()

    return Response(stream_with_context(generate()), status=resp.status_code, headers=headers)
