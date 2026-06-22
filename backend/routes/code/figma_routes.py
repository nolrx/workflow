"""
Figma integration routes (Code domain) — mounted at ``/api/code/figma``.

Surface:
- credential CRUD — store / read / delete the user's encrypted Figma PAT.
- resolve — validate a pasted Figma URL and return light file metadata.
- attach / get / detach design — pull a whole Figma file (all frames) and attach
  it to a Code project; a later ``code_frontend_project_generation`` run then
  builds the multi-file React project to match the design.
- export — build a plugin-consumable bundle (app -> Figma layers).
"""
import json
import logging

from flask import Blueprint, make_response, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from backend.extensions import db
from backend.models.code import (
    CodeFigmaDesign,
    CodeProject,
    FigmaCredential,
    FigmaExportPackage,
)
from backend.services import pricing
from backend.services.code.figma import storage as figma_storage
from backend.services.code.figma.crypto import encrypt_token, last4
from backend.services.code.figma_attach_service import attach_design
from backend.services.code.figma_export_service import (
    ExportError,
    build_html_export_payload,
    build_preview_export_payload,
    build_sliced_export_payload,
)
from backend.services.code.figma_service import FigmaError, FigmaService, parse_figma_url
from backend.services.credit_service import charge, refund_credits
from backend.utils.response import error_response, success_response

logger = logging.getLogger(__name__)

figma_bp = Blueprint("code_figma", __name__)


def _figma_error(exc: FigmaError):
    return error_response(exc.code, exc.message, exc.status)


@figma_bp.route("/credential", methods=["GET"])
@jwt_required()
def get_credential():
    """Return the current user's stored Figma credential (masked), or has_token=false."""
    user_id = get_jwt_identity()
    credential = FigmaCredential.query.filter_by(user_id=user_id).first()
    if not credential:
        return success_response({"has_token": False})
    return success_response(credential.to_dict())


@figma_bp.route("/credential", methods=["POST"])
@jwt_required()
def save_credential():
    """Validate a Figma PAT, then store it encrypted (one per user, UPSERT)."""
    user_id = get_jwt_identity()
    data = request.get_json() or {}
    token = (data.get("token") or "").strip()
    label = (data.get("label") or "").strip() or None
    if not token:
        return error_response("VALIDATION_ERROR", "请填写 Figma 个人访问令牌", 400)

    # Verify the token works before persisting anything.
    try:
        FigmaService(token).validate_token()
    except FigmaError as exc:
        return _figma_error(exc)

    credential = FigmaCredential.query.filter_by(user_id=user_id).first()
    if not credential:
        credential = FigmaCredential(user_id=user_id)
        db.session.add(credential)
    credential.team_id = data.get("team_id")
    credential.token_encrypted = encrypt_token(token)
    credential.token_last4 = last4(token)
    credential.label = label
    db.session.commit()
    return success_response(credential.to_dict(), "Figma 已连接", 201)


@figma_bp.route("/credential", methods=["DELETE"])
@jwt_required()
def delete_credential():
    """Remove the stored Figma credential."""
    user_id = get_jwt_identity()
    credential = FigmaCredential.query.filter_by(user_id=user_id).first()
    if credential:
        db.session.delete(credential)
        db.session.commit()
    return success_response({"has_token": False}, "已断开 Figma 连接")


@figma_bp.route("/resolve", methods=["POST"])
@jwt_required()
def resolve_url():
    """Parse a Figma URL + fetch light file metadata for a pre-import preview."""
    user_id = get_jwt_identity()
    data = request.get_json() or {}
    figma_url = (data.get("figma_url") or "").strip()
    if not figma_url:
        return error_response("VALIDATION_ERROR", "请粘贴 Figma 链接", 400)

    credential = FigmaCredential.query.filter_by(user_id=user_id).first()
    if not credential:
        return error_response("FIGMA_NOT_CONNECTED", "尚未连接 Figma，请先粘贴访问令牌", 400)

    from backend.services.code.figma.crypto import FigmaTokenDecryptError, decrypt_token

    try:
        token = decrypt_token(credential.token_encrypted)
    except FigmaTokenDecryptError:
        return error_response("FIGMA_CREDENTIAL_INVALID", "Figma 凭据已失效，请重新粘贴令牌", 400)

    try:
        file_key, node_id = parse_figma_url(figma_url)
        meta = FigmaService(token).get_file(file_key, depth=1)
    except FigmaError as exc:
        return _figma_error(exc)

    return success_response(
        {
            "file_key": file_key,
            "node_id": node_id,
            "name": meta.get("name"),
            "thumbnail_url": meta.get("thumbnailUrl"),
            "last_modified": meta.get("lastModified"),
        }
    )


# --- Attach a Figma design to a project (drives multi-file project gen) ------
@figma_bp.route("/projects/<project_id>/attach", methods=["POST"])
@jwt_required()
def attach_design_route(project_id: str):
    """Pull a whole Figma file (all frames) and attach it to the project.

    No LLM: this fetches the node tree + renders every top-level frame and stores
    them, so a later ``code_frontend_project_generation`` run builds the React
    project to match the design. UPSERT — re-attaching replaces the prior design.
    """
    user_id = get_jwt_identity()
    project = CodeProject.query.filter_by(id=project_id, user_id=user_id).first()
    if not project:
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)

    data = request.get_json() or {}
    figma_url = (data.get("figma_url") or "").strip()
    if not figma_url:
        return error_response("VALIDATION_ERROR", "请粘贴 Figma 链接", 400)

    credential = FigmaCredential.query.filter_by(user_id=user_id).first()
    if not credential:
        return error_response("FIGMA_NOT_CONNECTED", "尚未连接 Figma，请先粘贴访问令牌", 400)

    from backend.services.code.figma.crypto import FigmaTokenDecryptError, decrypt_token

    try:
        token = decrypt_token(credential.token_encrypted)
    except FigmaTokenDecryptError:
        return error_response("FIGMA_CREDENTIAL_INVALID", "Figma 凭据已失效，请重新粘贴令牌", 400)

    try:
        design = attach_design(project, token, figma_url)
    except FigmaError as exc:
        return _figma_error(exc)

    return success_response({"design": design.to_dict()}, "已关联 Figma 设计")


@figma_bp.route("/projects/<project_id>/design", methods=["GET"])
@jwt_required()
def get_design_route(project_id: str):
    """Return the project's currently attached Figma design (or null)."""
    user_id = get_jwt_identity()
    project = CodeProject.query.filter_by(id=project_id, user_id=user_id).first()
    if not project:
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)
    design = CodeFigmaDesign.query.filter_by(project_id=project.id).first()
    return success_response({"design": design.to_dict() if design else None})


@figma_bp.route("/projects/<project_id>/design", methods=["DELETE"])
@jwt_required()
def detach_design_route(project_id: str):
    """Remove the project's attached Figma design (and its render images)."""
    user_id = get_jwt_identity()
    project = CodeProject.query.filter_by(id=project_id, user_id=user_id).first()
    if not project:
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)
    design = CodeFigmaDesign.query.filter_by(project_id=project.id).first()
    if design:
        db.session.delete(design)
        db.session.commit()
    figma_storage.clear_design_dir(project.id)
    return success_response({"deleted": project.id}, "已解除 Figma 设计关联")


# --- Export to Figma (via companion plugin) ---------------------------------
@figma_bp.route("/projects/<project_id>/export", methods=["POST"])
@jwt_required()
def export_to_figma(project_id: str):
    """Build a plugin-consumable export bundle and return a one-time pairing code.

    ``source`` selects what is exported:
    - ``preview_image`` (default): a generated preview PNG -> single image frame
      (deterministic, no model call, no charge).
    - ``html``: the generated frontend HTML -> AI-built layer tree (later phase).
    """
    user_id = get_jwt_identity()
    project = CodeProject.query.filter_by(id=project_id, user_id=user_id).first()
    if not project:
        return error_response("NOT_FOUND", "项目不存在或无权访问", 404)

    data = request.get_json() or {}
    source = (data.get("source") or "preview_image").strip()

    if source == "preview_image":
        # Deterministic, no model call — never charged.
        try:
            payload = build_preview_export_payload(project, data.get("preview_id"))
        except ExportError as exc:
            return error_response(exc.code, exc.message, exc.status)
    elif source == "sliced":
        # Editable slice payload produced by a code_figma_slice_generation run.
        # Deterministic here (the agent run already did + was charged for the
        # analysis); just wrap the stored payload into a pairing code.
        try:
            payload = build_sliced_export_payload(project, data.get("run_id"))
        except ExportError as exc:
            return error_response(exc.code, exc.message, exc.status)
    elif source == "html":
        # AI-built layer tree — charge up-front, refund on failure.
        if not charge(
            user_id=user_id,
            amount=pricing.CODE_FIGMA_EXPORT,
            operation="code_figma_export",
            resource_type="code_project",
            resource_id=project.id,
            team_id=project.team_id,
        ):
            return error_response("INSUFFICIENT_CREDITS", "积分不足，无法导出", 402)
        try:
            payload = build_html_export_payload(project)
        except ExportError as exc:
            refund_credits(
                user_id, pricing.CODE_FIGMA_EXPORT, "code_figma_export",
                "code_project", project.id, team_id=project.team_id,
            )
            return error_response(exc.code, exc.message, exc.status)
        except Exception:  # noqa: BLE001 - refund then re-raise to the error handler
            refund_credits(
                user_id, pricing.CODE_FIGMA_EXPORT, "code_figma_export",
                "code_project", project.id, team_id=project.team_id,
            )
            raise
    else:
        return error_response("VALIDATION_ERROR", f"暂不支持的导出来源: {source}", 400)

    package = FigmaExportPackage.create(
        user_id=user_id,
        project_id=project.id,
        payload_json=json.dumps(payload, ensure_ascii=False),
        source=source,
    )
    db.session.add(package)
    db.session.commit()
    return success_response(package.to_dict(), "导出包已生成", 201)


@figma_bp.route("/pull", methods=["GET"])
def pull_export():
    """Fetch an export payload by pairing code — UNAUTHENTICATED (plugin endpoint).

    Authentication is the high-entropy, single-use, short-lived pairing code
    itself: an iframe-sandboxed Figma plugin cannot send a bearer token. The code
    is consumed on first successful read.
    """
    code = (request.args.get("code") or "").strip().upper()
    if not code:
        return error_response("VALIDATION_ERROR", "缺少配对码", 400)

    package = FigmaExportPackage.query.filter_by(pairing_code=code).first()
    if not package:
        return error_response("NOT_FOUND", "配对码无效", 404)
    if not package.is_valid():
        return error_response("EXPIRED", "配对码已失效或已被使用", 410)

    package.consumed = True
    db.session.commit()
    try:
        payload = json.loads(package.payload_json)
    except (ValueError, TypeError):
        return error_response("SERVER_ERROR", "导出包数据损坏", 500)

    # The Figma plugin fetches this from a sandboxed (null-origin) iframe, so the
    # configured CORS allowlist won't match. Safe to open up: the endpoint is
    # gated by an unguessable, single-use, expiring pairing code and returns no
    # user-identifying data beyond the design the user just chose to export.
    response = make_response(success_response(payload))
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response
