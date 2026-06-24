"""
Admin routes — online management of editable system prompts.

All endpoints require an admin JWT (see ``admin_required``). Prompts live in
MongoDB (see ``backend/services/prompts``); these endpoints list, read, edit and
reset them. When MongoDB is unreachable, reads fall back to bundled defaults
(read-only) and writes return 503.
"""
import hmac
import os
from functools import wraps

from flask import Blueprint, request

from backend.services import lifecycle
from backend.services.mongo import is_available as mongo_available
from backend.services.prompts import MongoUnavailableError, prompt_store
from backend.utils.auth import admin_required, current_user
from backend.utils.response import error_response, success_response

admin_bp = Blueprint("admin", __name__)


def _deploy_token_required(fn):
    """Guard ops/lifecycle endpoints with a shared deploy token (not a user JWT).

    The deploy script — not an interactive admin — drives drain/undrain, so these
    endpoints authenticate with the ``DEPLOY_CONTROL_TOKEN`` env value sent in the
    ``X-Deploy-Token`` header (constant-time compared). When the token is unset the
    endpoints are disabled (403), so they stay inert unless ops explicitly enables them.
    """

    @wraps(fn)
    def wrapper(*args, **kwargs):
        expected = os.getenv("DEPLOY_CONTROL_TOKEN") or ""
        if not expected:
            return error_response("FORBIDDEN", "运维控制未启用（未配置 DEPLOY_CONTROL_TOKEN）", 403)
        provided = request.headers.get("X-Deploy-Token", "")
        if not (provided and hmac.compare_digest(provided, expected)):
            return error_response("FORBIDDEN", "无效的部署控制令牌", 403)
        return fn(*args, **kwargs)

    return wrapper


class _SafeDict(dict):
    """Mapping that yields "" for any missing key, for a safe .format dry-run."""

    def __missing__(self, key):  # noqa: D401
        return ""


def _validate_content(key: str, content: str) -> str | None:
    """Return an error message if ``content`` is invalid for ``key``, else None."""
    if not content or not content.strip():
        return "提示词内容不能为空"
    # Code templates consumed via str.format must keep balanced/escaped braces.
    # Templates using [[TOKEN]] placeholders are filled via str.replace and may
    # legitimately contain raw { } (JSX), so skip the .format check for those.
    if key.startswith("code/") and "[[" not in content:
        try:
            content.format_map(_SafeDict())
        except (ValueError, IndexError) as error:
            return (
                f"模板花括号不合法(.format 解析失败):{error}。"
                "字面花括号请使用 {{ }} 转义。"
            )
    return None


@admin_bp.route("/prompts", methods=["GET"])
@admin_required
def list_prompts():
    """List all editable prompts (optionally filtered by ?scope=)."""
    scope = request.args.get("scope") or None
    prompts = prompt_store.list_docs(scope=scope)
    return success_response({"prompts": prompts, "mongo_available": mongo_available()})


@admin_bp.route("/prompts/<path:key>", methods=["GET"])
@admin_required
def get_prompt(key: str):
    """Return one prompt with its current content and bundled default."""
    doc = prompt_store.get_doc(key)
    if doc is None:
        return error_response("NOT_FOUND", "提示词不存在", 404)
    return success_response({"prompt": doc, "mongo_available": mongo_available()})


@admin_bp.route("/prompts/<path:key>", methods=["PUT"])
@admin_required
def update_prompt(key: str):
    """Overwrite the content of one prompt."""
    if prompt_store.get_doc(key) is None:
        return error_response("NOT_FOUND", "提示词不存在", 404)
    data = request.get_json(silent=True) or {}
    content = data.get("content")
    if not isinstance(content, str):
        return error_response("VALIDATION_ERROR", "content 字段必填且必须为字符串", 400)
    invalid = _validate_content(key, content)
    if invalid:
        return error_response("VALIDATION_ERROR", invalid, 400)
    user = current_user()
    try:
        doc = prompt_store.update(key, content, updated_by=user.id if user else None)
    except MongoUnavailableError as error:
        return error_response("SERVICE_UNAVAILABLE", str(error), 503)
    return success_response({"prompt": doc}, message="提示词已更新")


@admin_bp.route("/prompts/<path:key>/reset", methods=["POST"])
@admin_required
def reset_prompt(key: str):
    """Restore one prompt to its bundled default."""
    if prompt_store.get_doc(key) is None:
        return error_response("NOT_FOUND", "提示词不存在", 404)
    try:
        doc = prompt_store.reset(key)
    except MongoUnavailableError as error:
        return error_response("SERVICE_UNAVAILABLE", str(error), 503)
    return success_response({"prompt": doc}, message="提示词已恢复默认")


# --- Lifecycle / graceful-drain (deploy-script driven) -----------------------
# These let the deploy script flip the live instance into DRAIN before it swaps
# the container, so no new background work is accepted during the shutdown window
# (in-flight runs resume on the new process). Token-guarded, not user-JWT, so an
# ops script can call them without minting an admin login. See
# backend/services/lifecycle.py and scripts/deploy-backend.sh.
@admin_bp.route("/lifecycle/drain", methods=["POST"])
@_deploy_token_required
def lifecycle_drain():
    """Stop accepting new runs on this instance (idempotent)."""
    lifecycle.begin_drain()
    return success_response({"draining": True}, "已进入排空模式：不再接受新任务")


@admin_bp.route("/lifecycle/undrain", methods=["POST"])
@_deploy_token_required
def lifecycle_undrain():
    """Resume accepting new runs (idempotent; e.g. a cancelled deploy)."""
    lifecycle.end_drain()
    return success_response({"draining": False}, "已退出排空模式")


@admin_bp.route("/lifecycle/status", methods=["GET"])
@_deploy_token_required
def lifecycle_status():
    """Report this instance's drain state."""
    return success_response({"draining": lifecycle.is_draining()})
