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

from flask import Blueprint, Response, redirect, request, send_from_directory

from backend.models.agent.run import AgentRun, AgentRunStatus
from backend.models.code import CodeProject
from backend.models.code.fullstack import CodeDeployment, DeploymentStatus
from backend.routes.code.fullstack_routes import APP_TOKEN_COOKIE
from backend.services.agent.files import artifact_abs_path
from backend.utils.preview_token import PREVIEW_TOKEN_TTL, mint_preview_token, preview_identity
from backend.utils.response import error_response

code_preview_bp = Blueprint("code_preview", __name__)

_FRONTEND_WORKFLOW = "code_frontend_project_generation"
# Built runs whose dist is worth serving (a degraded build is still 'completed'/
# 'partial' and ships a previewable dist).
_BUILT_STATUSES = (AgentRunStatus.COMPLETED, AgentRunStatus.PARTIAL)
# Cookie that re-authenticates the dist's token-less relative asset requests.
_PREVIEW_COOKIE = "fe_preview_token"
# Cookie/token lifetime: a minted preview token outlives the 30-min access token so
# a left-open preview tab keeps working (see backend/utils/preview_token.py).
_COOKIE_MAX_AGE = PREVIEW_TOKEN_TTL
# Static-only preview (no deployed backend): block all network egress.
_PREVIEW_CSP = "default-src 'self' 'unsafe-inline' data: blob:; connect-src 'none'"
# Deployed preview (a live backend exists): allow same-origin XHR/fetch so the
# served frontend can reach the backend via /app/<pid>/api (same origin).
_DEPLOYED_CSP = "default-src 'self' 'unsafe-inline' data: blob:; connect-src 'self'"


def _running_deployment(project_id: str, user_id: str) -> CodeDeployment | None:
    dep = CodeDeployment.query.filter_by(project_id=project_id, user_id=user_id).first()
    return dep if dep and dep.status == DeploymentStatus.RUNNING else None


def _inject_api_base(html: str, api_base: str) -> str:
    """Inject ``window.__API_BASE__`` / ``window.__WS_BASE__`` so the static build
    talks to the live backend over both HTTP and WebSocket.

    The generated frontend reads ``window.__API_BASE__`` (falling back to
    ``import.meta.env.VITE_API_BASE_URL`` / ``/api``) as its API root and
    ``window.__WS_BASE__`` as its WebSocket root. WS shares the SAME base as HTTP
    (``/app/<pid>/api``): nginx routes a request under that base to the container
    directly when it carries an ``Upgrade:`` header, so the generated frontend just
    swaps ``http(s)→ws(s)`` on the same URL — no rebuild needed.
    """
    snippet = (
        f'<script>window.__API_BASE__={api_base!r};'
        f'window.__WS_BASE__={api_base!r};</script>'
    )
    lower = html.lower()
    idx = lower.find("</head>")
    if idx != -1:
        return html[:idx] + snippet + html[idx:]
    idx = lower.find("<body")
    if idx != -1:
        end = html.find(">", idx)
        if end != -1:
            return html[: end + 1] + snippet + html[end + 1 :]
    return snippet + html


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
    """Serve the project's latest built frontend dist at a session-scoped path.

    Access is granted to the OWNER (proven by a ``?token=`` JWT / pinned cookie)
    OR to anyone when the project is marked public (``visibility == 'public'``).
    A public project is reachable anonymously — the unguessable project UUID is
    the share capability and the owner can revoke it by flipping visibility back.
    Only the built static site is exposed this way (never the source zip / other
    artifacts, which live on the JWT-only ``/api/agent`` routes).
    """
    project = CodeProject.query.filter_by(id=project_id).first()
    if not project:
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)

    query_token = request.args.get("token", "")
    token = query_token or request.cookies.get(_PREVIEW_COOKIE, "")
    # Accepts the one-shot ?token= access token (entry) or a minted, project-scoped
    # preview token (cookie). A non-decodable token just means an anonymous visitor.
    identity = preview_identity(token, f"project:{project_id}")

    is_owner = bool(identity) and identity == project.user_id
    is_public = project.visibility == "public"
    if not (is_owner or is_public):
        return error_response("FORBIDDEN", "无效的预览令牌", 403)

    # The deliverable is always resolved against the project OWNER: an anonymous
    # public visitor has no identity of their own.
    owner_id = project.user_id
    run = _latest_built_run(project_id, owner_id)
    if not run:
        return error_response("NOT_FOUND", "该项目尚未生成可预览的前端工程", 404)
    site_dir = artifact_abs_path(f"agent_runs/{run.id}/site")
    if not os.path.isdir(site_dir):
        return error_response("NOT_FOUND", "预览不存在或尚未生成", 404)

    deployment = _running_deployment(project_id, owner_id)

    # Owner entry request: pin the token into a path-scoped cookie, then redirect
    # to a clean (token-less) URL so the JWT stays out of the address bar/history.
    # Public anonymous visitors carry no token and skip this entirely.
    if query_token and is_owner:
        # Exchange the one-shot 30-min access token for a longer-lived token scoped
        # to THIS project, so a left-open preview tab keeps authenticating past the
        # access token's expiry (the deployed app polls /app/<pid>/api for hours).
        session_token = mint_preview_token(identity, f"project:{project_id}")
        response = redirect(f"/preview/{project_id}/")
        response.set_cookie(
            _PREVIEW_COOKIE, session_token, max_age=_COOKIE_MAX_AGE,
            httponly=True, samesite="Lax", path=f"/preview/{project_id}/",
        )
        # Also authenticate the served frontend's calls to /app/<pid>/api with a
        # path-scoped cookie — planted on EVERY owner entry, NOT gated on a running
        # deployment. The common sequence is: open preview, THEN deploy, THEN the
        # tab reloads token-lessly and the now-running deployment makes the build
        # call /app/<pid>/api/<...>. If the cookie were only set when a deployment
        # already existed at entry time, that later reload would carry no token and
        # the proxy would reject the API calls with 403 ("无效的访问令牌"). The cookie
        # is harmless before a backend exists: for a valid token with no running
        # deployment the proxy returns 404 ("后端尚未部署"), and the proxy re-checks
        # project ownership on every request regardless. Scoped to /app/<pid>/.
        response.set_cookie(
            APP_TOKEN_COOKIE, session_token, max_age=_COOKIE_MAX_AGE,
            httponly=True, samesite="Lax", path=f"/app/{project_id}/",
        )
        return response

    csp = _DEPLOYED_CSP if deployment else _PREVIEW_CSP

    # Inject the runtime API base into the entry HTML so the static build talks to
    # the live backend (no rebuild). Other assets are served as-is.
    is_index = filename in ("", "index.html") or filename.endswith("/index.html")
    if deployment and is_index:
        index_path = os.path.join(site_dir, "index.html")
        if os.path.isfile(index_path):
            with open(index_path, encoding="utf-8", errors="replace") as fh:
                html = _inject_api_base(fh.read(), deployment.api_base_path or f"/app/{project_id}/api")
            response = Response(html, mimetype="text/html; charset=utf-8")
            response.headers["Content-Security-Policy"] = csp
            return response

    response = send_from_directory(site_dir, filename)
    response.headers["Content-Security-Policy"] = csp
    return response
