"""
Admin routes — online management of editable system prompts.

All endpoints require an admin JWT (see ``admin_required``). Prompts live in
MongoDB (see ``backend/services/prompts``); these endpoints list, read, edit and
reset them. When MongoDB is unreachable, reads fall back to bundled defaults
(read-only) and writes return 503.
"""
from flask import Blueprint, request

from backend.services.mongo import is_available as mongo_available
from backend.services.prompts import MongoUnavailableError, prompt_store
from backend.utils.auth import admin_required, current_user
from backend.utils.response import error_response, success_response

admin_bp = Blueprint("admin", __name__)


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
