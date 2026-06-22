"""
GitHub integration routes (Code domain) — mounted at ``/api/code/github``.

Read-only surface (sync itself is automatic, fired by the agent runtime when a
``code_*`` run completes — there is no manual push endpoint):
- ``GET /status`` — whether the org-level GitHub App is configured + reachable.
- ``GET /projects/<id>/repo`` — the session's repo link + latest push summary.
- ``GET /projects/<id>/pushes`` — push history for the session.
"""
import logging

from flask import Blueprint
from flask_jwt_extended import get_jwt_identity, jwt_required

from backend.models.code import CodeProject, GitHubPushLog, GitHubRepoLink
from backend.services.code.github import app_auth
from backend.services.code.github.client import GitHubError
from backend.utils.response import error_response, success_response

logger = logging.getLogger(__name__)

github_bp = Blueprint("code_github", __name__)


def _get_owned_project(project_id: str) -> CodeProject | None:
    user_id = get_jwt_identity()
    return CodeProject.query.filter_by(id=project_id, user_id=user_id).first()


@github_bp.route("/status", methods=["GET"])
@jwt_required()
def status():
    """Report whether GitHub auto-sync is configured and the App is reachable."""
    if not app_auth.is_configured():
        return success_response({"configured": False, "connected": False})
    try:
        installation_id, account = app_auth.resolve_installation()
        owner = app_auth.repo_owner(account)
        return success_response(
            {
                "configured": True,
                "connected": True,
                "owner": owner,
                "installation_id": installation_id,
            }
        )
    except GitHubError as exc:
        return success_response(
            {"configured": True, "connected": False, "error": exc.message}
        )


@github_bp.route("/projects/<project_id>/repo", methods=["GET"])
@jwt_required()
def get_project_repo(project_id: str):
    """Return the session's GitHub repo link + its most recent push, if any."""
    project = _get_owned_project(project_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在", 404)

    link = GitHubRepoLink.query.filter_by(project_id=project_id).first()
    last_push = (
        GitHubPushLog.query.filter_by(project_id=project_id)
        .order_by(GitHubPushLog.created_at.desc())
        .first()
    )
    return success_response(
        {
            "configured": app_auth.is_configured(),
            "linked": bool(link),
            "repo": link.to_dict() if link else None,
            "last_push": last_push.to_dict() if last_push else None,
        }
    )


@github_bp.route("/projects/<project_id>/pushes", methods=["GET"])
@jwt_required()
def list_project_pushes(project_id: str):
    """Return the session's push history (most recent first)."""
    project = _get_owned_project(project_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在", 404)

    pushes = (
        GitHubPushLog.query.filter_by(project_id=project_id)
        .order_by(GitHubPushLog.created_at.desc())
        .limit(50)
        .all()
    )
    return success_response({"pushes": [push.to_dict() for push in pushes]})
