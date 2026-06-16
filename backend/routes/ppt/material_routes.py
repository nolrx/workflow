"""
PPT Material routes - handles material image upload and management
"""
import logging
import time
from pathlib import Path

from flask import Blueprint, current_app, request
from flask_jwt_extended import get_jwt_identity, jwt_required
from werkzeug.utils import secure_filename

from backend.extensions import db
from backend.models.ppt import PPTMaterial, PPTProject
from backend.utils.response import error_response, success_response

logger = logging.getLogger(__name__)

ppt_material_bp = Blueprint("ppt_materials", __name__)

ALLOWED_MATERIAL_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


def _get_upload_folder():
    """Get upload folder path"""
    return Path(current_app.config.get("UPLOAD_FOLDER", "uploads"))


def _is_allowed_file(filename: str) -> bool:
    """Check if file extension is allowed"""
    if "." not in filename:
        return False
    ext = "." + filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_MATERIAL_EXTENSIONS


@ppt_material_bp.route("/projects/<project_id>/materials", methods=["GET"])
@jwt_required()
def list_project_materials(project_id: str):
    """
    GET /api/ppt/projects/{project_id}/materials - List materials for a project

    Returns:
        List of material images for the specified project
    """
    try:
        user_id = get_jwt_identity()

        # Verify project ownership
        project = PPTProject.query.get(project_id)
        if not project:
            return error_response("NOT_FOUND", "Project not found", 404)

        if project.user_id != user_id:
            # Check team membership if team project
            if project.team_id:
                from backend.models.team import TeamMember
                membership = TeamMember.query.filter_by(
                    team_id=project.team_id, user_id=user_id
                ).first()
                if not membership:
                    return error_response("FORBIDDEN", "Access denied", 403)
            else:
                return error_response("FORBIDDEN", "Access denied", 403)

        materials = PPTMaterial.query.filter_by(project_id=project_id).order_by(
            PPTMaterial.created_at.desc()
        ).all()

        return success_response({
            "materials": [m.to_dict() for m in materials],
            "count": len(materials)
        })

    except Exception as e:
        logger.error(f"list_project_materials failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_material_bp.route("/projects/<project_id>/materials/upload", methods=["POST"])
@jwt_required()
def upload_material(project_id: str):
    """
    POST /api/ppt/projects/{project_id}/materials/upload - Upload a material image

    Supports multipart/form-data:
    - file: Image file (required)

    Returns:
        Material info with filename, url, and metadata
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
        if "file" not in request.files:
            return error_response("BAD_REQUEST", "No file provided", 400)

        file = request.files["file"]
        if not file or not file.filename:
            return error_response("BAD_REQUEST", "No file selected", 400)

        filename = secure_filename(file.filename)
        if not _is_allowed_file(filename):
            return error_response(
                "BAD_REQUEST",
                f"Unsupported file type. Allowed: {', '.join(sorted(ALLOWED_MATERIAL_EXTENSIONS))}",
                400
            )

        # Create materials directory
        upload_folder = _get_upload_folder()
        materials_dir = upload_folder / "ppt" / project_id / "materials"
        materials_dir.mkdir(parents=True, exist_ok=True)

        # Generate unique filename
        timestamp = int(time.time() * 1000)
        base_name = Path(filename).stem
        ext = Path(filename).suffix.lower()
        unique_filename = f"{base_name}_{timestamp}{ext}"

        # Save file
        filepath = materials_dir / unique_filename
        file.save(str(filepath))

        # Create relative path and URL
        relative_path = str(filepath.relative_to(upload_folder))
        url = f"/api/ppt/files/{project_id}/materials/{unique_filename}"

        # Create database record
        material = PPTMaterial(
            user_id=user_id,
            project_id=project_id,
            filename=unique_filename,
            relative_path=relative_path,
            url=url
        )

        db.session.add(material)
        db.session.commit()

        return success_response(material.to_dict(), status_code=201)

    except Exception as e:
        db.session.rollback()
        logger.error(f"upload_material failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_material_bp.route("/materials", methods=["GET"])
@jwt_required()
def list_user_materials():
    """
    GET /api/ppt/materials - List all materials for current user

    Query params:
        - project_id: Filter by project (optional)
          * 'all' (default): Get all user's materials
          * 'global': Get only global materials (not associated with any project)
          * <project_id>: Get materials for specific project

    Returns:
        List of material images
    """
    try:
        user_id = get_jwt_identity()
        filter_project_id = request.args.get("project_id", "all")

        query = PPTMaterial.query.filter_by(user_id=user_id)

        if filter_project_id == "global":
            query = query.filter(PPTMaterial.project_id.is_(None))
        elif filter_project_id != "all":
            query = query.filter(PPTMaterial.project_id == filter_project_id)

        materials = query.order_by(PPTMaterial.created_at.desc()).all()

        return success_response({
            "materials": [m.to_dict() for m in materials],
            "count": len(materials)
        })

    except Exception as e:
        logger.error(f"list_user_materials failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_material_bp.route("/materials/upload", methods=["POST"])
@jwt_required()
def upload_global_material():
    """
    POST /api/ppt/materials/upload - Upload a global material image (not bound to a project)

    Supports multipart/form-data:
    - file: Image file (required)

    Returns:
        Material info with filename, url, and metadata
    """
    try:
        user_id = get_jwt_identity()

        # Check file
        if "file" not in request.files:
            return error_response("BAD_REQUEST", "No file provided", 400)

        file = request.files["file"]
        if not file or not file.filename:
            return error_response("BAD_REQUEST", "No file selected", 400)

        filename = secure_filename(file.filename)
        if not _is_allowed_file(filename):
            return error_response(
                "BAD_REQUEST",
                f"Unsupported file type. Allowed: {', '.join(sorted(ALLOWED_MATERIAL_EXTENSIONS))}",
                400
            )

        # Create global materials directory
        upload_folder = _get_upload_folder()
        materials_dir = upload_folder / "ppt" / "global" / "materials"
        materials_dir.mkdir(parents=True, exist_ok=True)

        # Generate unique filename
        timestamp = int(time.time() * 1000)
        base_name = Path(filename).stem
        ext = Path(filename).suffix.lower()
        unique_filename = f"{base_name}_{timestamp}{ext}"

        # Save file
        filepath = materials_dir / unique_filename
        file.save(str(filepath))

        # Create relative path and URL
        relative_path = str(filepath.relative_to(upload_folder))
        url = f"/api/ppt/files/global/materials/{unique_filename}"

        # Create database record (no project_id)
        material = PPTMaterial(
            user_id=user_id,
            project_id=None,
            filename=unique_filename,
            relative_path=relative_path,
            url=url
        )

        db.session.add(material)
        db.session.commit()

        return success_response(material.to_dict(), status_code=201)

    except Exception as e:
        db.session.rollback()
        logger.error(f"upload_global_material failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_material_bp.route("/materials/<material_id>", methods=["DELETE"])
@jwt_required()
def delete_material(material_id: str):
    """
    DELETE /api/ppt/materials/{material_id} - Delete a material

    Returns:
        Success message
    """
    try:
        user_id = get_jwt_identity()

        material = PPTMaterial.query.get(material_id)
        if not material:
            return error_response("NOT_FOUND", "Material not found", 404)

        # Verify ownership
        if material.user_id != user_id:
            return error_response("FORBIDDEN", "Access denied", 403)

        # Delete file from disk
        upload_folder = _get_upload_folder()
        filepath = upload_folder / material.relative_path

        # Delete database record first
        db.session.delete(material)
        db.session.commit()

        # Then try to delete file (non-critical if fails)
        try:
            if filepath.exists():
                filepath.unlink(missing_ok=True)
        except OSError as e:
            logger.warning(f"Failed to delete material file {material_id}: {e}")

        return success_response({"id": material_id}, message="Material deleted successfully")

    except Exception as e:
        db.session.rollback()
        logger.error(f"delete_material failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_material_bp.route("/materials/associate", methods=["POST"])
@jwt_required()
def associate_materials():
    """
    POST /api/ppt/materials/associate - Associate materials to a project by URLs

    Request body:
    {
        "project_id": "project-id",
        "material_urls": ["url1", "url2", ...]
    }

    Returns:
        List of associated material IDs
    """
    try:
        user_id = get_jwt_identity()
        data = request.get_json() or {}

        project_id = data.get("project_id")
        material_urls = data.get("material_urls", [])

        if not project_id:
            return error_response("BAD_REQUEST", "project_id is required", 400)

        if not material_urls or not isinstance(material_urls, list):
            return error_response("BAD_REQUEST", "material_urls must be a non-empty array", 400)

        # Verify project ownership
        project = PPTProject.query.get(project_id)
        if not project:
            return error_response("NOT_FOUND", "Project not found", 404)

        if project.user_id != user_id:
            return error_response("FORBIDDEN", "Access denied", 403)

        # Find materials by URLs and update their project_id
        updated_ids = []
        materials = PPTMaterial.query.filter(
            PPTMaterial.url.in_(material_urls),
            PPTMaterial.user_id == user_id,
            PPTMaterial.project_id.is_(None)
        ).all()

        for material in materials:
            material.project_id = project_id
            updated_ids.append(material.id)

        db.session.commit()

        return success_response({
            "updated_ids": updated_ids,
            "count": len(updated_ids)
        })

    except Exception as e:
        db.session.rollback()
        logger.error(f"associate_materials failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)
