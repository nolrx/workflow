"""
PPT Template routes - handles user template upload and management
"""
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import Blueprint, current_app, request, send_file
from flask_jwt_extended import get_jwt_identity, jwt_required
from werkzeug.utils import safe_join, secure_filename

from backend.extensions import db
from backend.models.ppt import PPTProject, PPTUserTemplate
from backend.utils.response import error_response, success_response

logger = logging.getLogger(__name__)

ppt_template_bp = Blueprint("ppt_templates", __name__)

ALLOWED_TEMPLATE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pptx"}


def _get_upload_folder():
    """Get upload folder path"""
    return Path(current_app.config.get("UPLOAD_FOLDER", "uploads"))


def _is_allowed_file(filename: str) -> bool:
    """Check if file extension is allowed"""
    if "." not in filename:
        return False
    ext = "." + filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_TEMPLATE_EXTENSIONS


# ========== Project Template Endpoints ==========

@ppt_template_bp.route("/projects/<project_id>/template", methods=["POST"])
@jwt_required()
def upload_project_template(project_id: str):
    """
    POST /api/ppt/projects/{project_id}/template - Upload template image for a project

    Content-Type: multipart/form-data
    Form: template_image=@file.png

    Returns:
        Template image URL
    """
    try:
        user_id = get_jwt_identity()

        # Verify project ownership
        project = PPTProject.query.get(project_id)
        if not project:
            return error_response("NOT_FOUND", "Project not found", 404)

        if project.user_id != user_id:
            return error_response("FORBIDDEN", "Access denied", 403)

        # Check file
        if "template_image" not in request.files:
            return error_response("BAD_REQUEST", "No file uploaded", 400)

        file = request.files["template_image"]
        if not file or file.filename == "":
            return error_response("BAD_REQUEST", "No file selected", 400)

        filename = secure_filename(file.filename)
        if not _is_allowed_file(filename):
            return error_response(
                "BAD_REQUEST",
                f"Invalid file type. Allowed: {', '.join(sorted(ALLOWED_TEMPLATE_EXTENSIONS))}",
                400
            )

        # Create template directory
        upload_folder = _get_upload_folder()
        template_dir = upload_folder / "ppt" / project_id / "template"
        template_dir.mkdir(parents=True, exist_ok=True)

        # Generate unique filename
        timestamp = int(time.time() * 1000)
        base_name = Path(filename).stem
        ext = Path(filename).suffix.lower()
        unique_filename = f"template_{timestamp}{ext}"

        # Save file
        filepath = template_dir / unique_filename
        file.save(str(filepath))

        # Update project
        relative_path = str(filepath.relative_to(upload_folder))
        project.template_image_path = relative_path
        project.updated_at = datetime.utcnow()

        db.session.commit()

        return success_response({
            "template_image_url": f"/api/ppt/files/{project_id}/template/{unique_filename}"
        })

    except Exception as e:
        db.session.rollback()
        logger.error(f"upload_project_template failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_template_bp.route("/projects/<project_id>/template", methods=["DELETE"])
@jwt_required()
def delete_project_template(project_id: str):
    """
    DELETE /api/ppt/projects/{project_id}/template - Delete project template

    Returns:
        Success message
    """
    try:
        user_id = get_jwt_identity()

        # Verify project ownership
        project = PPTProject.query.get(project_id)
        if not project:
            return error_response("NOT_FOUND", "Project not found", 404)

        if project.user_id != user_id:
            return error_response("FORBIDDEN", "Access denied", 403)

        if not project.template_image_path:
            return error_response("BAD_REQUEST", "No template to delete", 400)

        # Delete template file
        upload_folder = _get_upload_folder()
        filepath = upload_folder / project.template_image_path

        try:
            if filepath.exists():
                filepath.unlink()
        except OSError as e:
            logger.warning(f"Failed to delete template file: {e}")

        # Update project
        project.template_image_path = None
        project.updated_at = datetime.utcnow()

        db.session.commit()

        return success_response(message="Template deleted successfully")

    except Exception as e:
        db.session.rollback()
        logger.error(f"delete_project_template failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


# ========== User Template Endpoints ==========

@ppt_template_bp.route("/user-templates", methods=["POST"])
@jwt_required()
def upload_user_template():
    """
    POST /api/ppt/user-templates - Upload user template image

    Content-Type: multipart/form-data
    Form:
    - template_image: Image file (required)
    - name: Template name (optional)
    - visibility: private|team|public (optional, default: private)

    Returns:
        Template info
    """
    try:
        user_id = get_jwt_identity()

        # Check file
        if "template_image" not in request.files:
            return error_response("BAD_REQUEST", "No file uploaded", 400)

        file = request.files["template_image"]
        if not file or file.filename == "":
            return error_response("BAD_REQUEST", "No file selected", 400)

        filename = secure_filename(file.filename)
        if not _is_allowed_file(filename):
            return error_response(
                "BAD_REQUEST",
                f"Invalid file type. Allowed: {', '.join(sorted(ALLOWED_TEMPLATE_EXTENSIONS))}",
                400
            )

        # Get optional parameters
        name = request.form.get("name")
        visibility = request.form.get("visibility", "private")
        team_id = request.form.get("team_id")

        if visibility not in ["private", "team", "public"]:
            visibility = "private"

        # Get file size
        file.seek(0, 2)
        file_size = file.tell()
        file.seek(0)

        # Generate template ID
        template_id = str(uuid.uuid4())

        # Create template directory
        upload_folder = _get_upload_folder()
        template_dir = upload_folder / "ppt" / "user-templates" / template_id
        template_dir.mkdir(parents=True, exist_ok=True)

        # Generate filename
        ext = Path(filename).suffix.lower()
        unique_filename = f"template{ext}"

        # Save file
        filepath = template_dir / unique_filename
        file.save(str(filepath))

        # Create template record
        relative_path = str(filepath.relative_to(upload_folder))
        template = PPTUserTemplate(
            id=template_id,
            user_id=user_id,
            team_id=team_id if visibility == "team" else None,
            name=name,
            file_path=relative_path,
            file_size=file_size,
            visibility=visibility
        )

        db.session.add(template)
        db.session.commit()

        return success_response(template.to_dict(), status_code=201)

    except Exception as e:
        db.session.rollback()
        logger.error(f"upload_user_template failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_template_bp.route("/user-templates", methods=["GET"])
@jwt_required()
def list_user_templates():
    """
    GET /api/ppt/user-templates - List user templates

    Query params:
    - visibility: Filter by visibility (private, team, public, all)
    - team_id: Filter by team (optional)

    Returns:
        List of templates
    """
    try:
        user_id = get_jwt_identity()
        visibility = request.args.get("visibility", "all")
        team_id = request.args.get("team_id")

        # Start with user's own templates
        query = PPTUserTemplate.query.filter(
            (PPTUserTemplate.user_id == user_id) |
            (PPTUserTemplate.visibility == "public")
        )

        # Add team templates if team_id provided
        if team_id:
            query = PPTUserTemplate.query.filter(
                (PPTUserTemplate.user_id == user_id) |
                (PPTUserTemplate.visibility == "public") |
                ((PPTUserTemplate.team_id == team_id) & (PPTUserTemplate.visibility == "team"))
            )

        # Filter by visibility
        if visibility != "all":
            if visibility == "private":
                query = query.filter(
                    PPTUserTemplate.user_id == user_id,
                    PPTUserTemplate.visibility == "private"
                )
            elif visibility == "team":
                query = query.filter(PPTUserTemplate.visibility == "team")
            elif visibility == "public":
                query = query.filter(PPTUserTemplate.visibility == "public")

        templates = query.order_by(PPTUserTemplate.created_at.desc()).all()

        return success_response({
            "templates": [t.to_dict() for t in templates]
        })

    except Exception as e:
        logger.error(f"list_user_templates failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_template_bp.route("/user-templates/<template_id>", methods=["GET"])
@jwt_required()
def get_user_template(template_id: str):
    """
    GET /api/ppt/user-templates/{template_id} - Get template details

    Returns:
        Template info
    """
    try:
        user_id = get_jwt_identity()

        template = PPTUserTemplate.query.get(template_id)
        if not template:
            return error_response("NOT_FOUND", "Template not found", 404)

        # Check access
        if template.visibility == "private" and template.user_id != user_id:
            return error_response("FORBIDDEN", "Access denied", 403)

        if template.visibility == "team" and template.team_id:
            from backend.models.team import TeamMember
            membership = TeamMember.query.filter_by(
                team_id=template.team_id, user_id=user_id
            ).first()
            if not membership and template.user_id != user_id:
                return error_response("FORBIDDEN", "Access denied", 403)

        return success_response(template.to_dict())

    except Exception as e:
        logger.error(f"get_user_template failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_template_bp.route("/user-templates/<template_id>", methods=["PUT"])
@jwt_required()
def update_user_template(template_id: str):
    """
    PUT /api/ppt/user-templates/{template_id} - Update template

    Request body:
    {
        "name": "New name",
        "visibility": "private|team|public"
    }

    Returns:
        Updated template info
    """
    try:
        user_id = get_jwt_identity()

        template = PPTUserTemplate.query.get(template_id)
        if not template:
            return error_response("NOT_FOUND", "Template not found", 404)

        # Only owner can update
        if template.user_id != user_id:
            return error_response("FORBIDDEN", "Access denied", 403)

        data = request.get_json() or {}

        if "name" in data:
            template.name = data["name"]

        if "visibility" in data:
            if data["visibility"] in ["private", "team", "public"]:
                template.visibility = data["visibility"]

        template.updated_at = datetime.utcnow()
        db.session.commit()

        return success_response(template.to_dict())

    except Exception as e:
        db.session.rollback()
        logger.error(f"update_user_template failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_template_bp.route("/user-templates/<template_id>", methods=["DELETE"])
@jwt_required()
def delete_user_template(template_id: str):
    """
    DELETE /api/ppt/user-templates/{template_id} - Delete template

    Returns:
        Success message
    """
    try:
        user_id = get_jwt_identity()

        template = PPTUserTemplate.query.get(template_id)
        if not template:
            return error_response("NOT_FOUND", "Template not found", 404)

        # Only owner can delete
        if template.user_id != user_id:
            return error_response("FORBIDDEN", "Access denied", 403)

        # Delete template file
        upload_folder = _get_upload_folder()
        filepath = upload_folder / template.file_path

        # Delete template directory
        template_dir = filepath.parent
        try:
            if filepath.exists():
                filepath.unlink()
            if template_dir.exists() and not any(template_dir.iterdir()):
                template_dir.rmdir()
        except OSError as e:
            logger.warning(f"Failed to delete template files: {e}")

        # Delete record
        db.session.delete(template)
        db.session.commit()

        return success_response(message="Template deleted successfully")

    except Exception as e:
        db.session.rollback()
        logger.error(f"delete_user_template failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


# ========== File Serving ==========

@ppt_template_bp.route("/files/user-templates/<template_id>/<filename>")
def serve_user_template_image(template_id: str, filename: str):
    """
    GET /api/ppt/files/user-templates/{template_id}/{filename}
    Serve user template images (no auth required for public templates)
    """
    try:
        upload_folder = _get_upload_folder()
        template_dir = upload_folder / "ppt" / "user-templates" / template_id

        # Security: prevent path traversal
        filepath = safe_join(str(template_dir), filename)
        if not filepath or not os.path.exists(filepath):
            return {"success": False, "error": "NOT_FOUND", "message": "File not found"}, 404

        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "png"
        mimetype = f"image/{ext}" if ext in ["png", "jpg", "jpeg", "gif", "webp"] else "application/octet-stream"

        return send_file(filepath, mimetype=mimetype)

    except Exception as e:
        logger.error(f"serve_user_template_image failed: {str(e)}", exc_info=True)
        return {"success": False, "error": "SERVER_ERROR", "message": str(e)}, 500
