"""
Session-bound deployed-preview routes for Code frontend projects.

A generated frontend project (the ``code_frontend_project_generation`` workflow)
publishes its built ``dist`` under ``agent_runs/<run_id>/site/``. The agent panel
already previews that build inside a sandboxed iframe in the chat. This blueprint
exposes the *same* build at a clean, session-scoped URL so it can be opened
directly in a real browser tab (full-page, native — not folded into the
transcript):

    /preview/<project_id>/[<path>]

The URL is keyed by the Code project (the "session"), NOT the run id, so it is
stable across regenerations: it always resolves to the project's most recent
successfully-built frontend run. Mounting at the top level (``/preview`` rather
than ``/api/...``) keeps it deploy-like; nginx proxies this prefix to the backend
(see ``frontend/nginx/default.conf``).

Auth mirrors ``agent_routes.serve_run_site``: a browser tab cannot send an
``Authorization`` header, so ownership is proven by a one-shot ``?token=`` JWT on
the entry request. We pin it into a short-lived, path-scoped cookie and 302 to a
token-less URL, so the JWT never lingers in the address bar / history while the
dist's relative asset requests stay authenticated via the cookie.
"""
import os

from flask import Blueprint, redirect, request, send_from_directory
from flask_jwt_extended import decode_token

from backend.models.agent.run import AgentRun, AgentRunStatus
from backend.models.code import CodeProject
from backend.services.agent.files import artifact_abs_path
from backend.utils.response import error_response

code_preview_bp = Blueprint("code_preview", __name__)

_FRONTEND_WORKFLOW = "code_frontend_project_generation"
# Built runs whose dist is worth serving (a degraded build is still 'completed'/
# 'partial' and ships a previewable dist).
_BUILT_STATUSES = (AgentRunStatus.COMPLETED, AgentRunStatus.PARTIAL)
# Cookie that re-authenticates the dist's token-less relative asset requests.
_PREVIEW_COOKIE = "fe_preview_token"
_COOKIE_MAX_AGE = 1800
# Defense-in-depth for the deployed preview: block fetch/XHR/WebSocket egress (the
# agent-generated app is fully static and needs no network) while still allowing
# the same-origin assets + inline modulepreload a normal Vite build emits.
_PREVIEW_CSP = "default-src 'self' 'unsafe-inline' data: blob:; connect-src 'none'"


def _latest_built_run(project_id: str, user_id: str) -> AgentRun | None:
    """Most recent successfully-built frontend run for this project (owner-scoped)."""
    return (
        AgentRun.query.filter_by(
            resource_id=project_id, user_id=user_id, workflow=_FRONTEND_WORKFLOW
        )
        .filter(AgentRun.status.in_(_BUILT_STATUSES))
        .order_by(AgentRun.created_at.desc())
        .first()
    )


@code_preview_bp.route("/<project_id>/", methods=["GET"])
@code_preview_bp.route("/<project_id>/<path:filename>", methods=["GET"])
def serve_project_preview(project_id: str, filename: str = "index.html"):
    """Serve the project's latest built frontend dist at a session-scoped path."""
    query_token = request.args.get("token", "")
    token = query_token or request.cookies.get(_PREVIEW_COOKIE, "")
    try:
        identity = decode_token(token).get("sub")
    except Exception:  # noqa: BLE001 - any decode failure is simply a rejected preview
        return error_response("FORBIDDEN", "无效的预览令牌", 403)

    project = CodeProject.query.filter_by(id=project_id, user_id=identity).first()
    if not project:
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)

    run = _latest_built_run(project_id, identity)
    if not run:
        return error_response("NOT_FOUND", "该项目尚未生成可预览的前端工程", 404)
    site_dir = artifact_abs_path(f"agent_runs/{run.id}/site")
    if not os.path.isdir(site_dir):
        return error_response("NOT_FOUND", "预览不存在或尚未生成", 404)

    # Entry request: pin the token into a path-scoped cookie, then redirect to a
    # clean (token-less) URL so the JWT stays out of the address bar / history.
    if query_token:
        response = redirect(f"/preview/{project_id}/")
        response.set_cookie(
            _PREVIEW_COOKIE,
            query_token,
            max_age=_COOKIE_MAX_AGE,
            httponly=True,
            samesite="Lax",
            path=f"/preview/{project_id}/",
        )
        return response

    response = send_from_directory(site_dir, filename)
    response.headers["Content-Security-Policy"] = _PREVIEW_CSP
    return response
