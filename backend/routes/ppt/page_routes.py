"""
PPT Page routes - handles page-related endpoints with JWT authentication
"""
import base64
import logging
import os
from datetime import datetime
from pathlib import Path

from flask import Blueprint, current_app, request
from flask_jwt_extended import get_jwt_identity, jwt_required
from sqlalchemy import func

from backend.extensions import db
from backend.models.ppt import PPTPage, PPTPageImageVersion, PPTProject
from backend.utils.response import error_response, success_response

logger = logging.getLogger(__name__)

ppt_page_bp = Blueprint("ppt_pages", __name__)


def _verify_project_access(project_id: str, user_id: str):
    """Verify user has access to the project"""
    project = PPTProject.query.get(project_id)
    if not project:
        return None, error_response("NOT_FOUND", "Project not found", 404)
    if project.user_id != user_id:
        return None, error_response("FORBIDDEN", "Access denied", 403)
    return project, None


@ppt_page_bp.route("/<project_id>/pages", methods=["GET"])
@jwt_required()
def list_pages(project_id: str):
    """
    GET /api/ppt/projects/{project_id}/pages - List project pages
    """
    try:
        user_id = get_jwt_identity()

        project, error = _verify_project_access(project_id, user_id)
        if error:
            return error

        pages = PPTPage.query\
            .filter_by(project_id=project_id)\
            .order_by(PPTPage.order_index)\
            .all()

        return success_response({
            "pages": [page.to_dict() for page in pages]
        })

    except Exception as e:
        logger.error(f"list_pages failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_page_bp.route("/<project_id>/pages/<page_id>", methods=["GET"])
@jwt_required()
def get_page(project_id: str, page_id: str):
    """
    GET /api/ppt/projects/{project_id}/pages/{page_id} - Get page details
    """
    try:
        user_id = get_jwt_identity()

        project, error = _verify_project_access(project_id, user_id)
        if error:
            return error

        page = PPTPage.query.filter_by(
            id=page_id,
            project_id=project_id
        ).first()

        if not page:
            return error_response("NOT_FOUND", "Page not found", 404)

        return success_response(page.to_dict(include_versions=True))

    except Exception as e:
        logger.error(f"get_page failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_page_bp.route("/<project_id>/pages/<page_id>", methods=["PUT"])
@jwt_required()
def update_page(project_id: str, page_id: str):
    """
    PUT /api/ppt/projects/{project_id}/pages/{page_id} - Update page

    Request body:
    {
        "outline_content": {"title": "...", "points": [...]},
        "description_content": {"text": "..."},
        "part": "..."
    }
    """
    try:
        user_id = get_jwt_identity()

        project, error = _verify_project_access(project_id, user_id)
        if error:
            return error

        page = PPTPage.query.filter_by(
            id=page_id,
            project_id=project_id
        ).first()

        if not page:
            return error_response("NOT_FOUND", "Page not found", 404)

        data = request.get_json()

        if "outline_content" in data:
            page.set_outline_content(data["outline_content"])

        if "description_content" in data:
            page.set_description_content(data["description_content"])

        if "part" in data:
            page.part = data["part"]

        page.updated_at = datetime.utcnow()
        project.updated_at = datetime.utcnow()
        db.session.commit()

        return success_response(page.to_dict())

    except Exception as e:
        db.session.rollback()
        logger.error(f"update_page failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_page_bp.route("/<project_id>/pages/<page_id>", methods=["DELETE"])
@jwt_required()
def delete_page(project_id: str, page_id: str):
    """
    DELETE /api/ppt/projects/{project_id}/pages/{page_id} - Delete page
    """
    try:
        user_id = get_jwt_identity()

        project, error = _verify_project_access(project_id, user_id)
        if error:
            return error

        page = PPTPage.query.filter_by(
            id=page_id,
            project_id=project_id
        ).first()

        if not page:
            return error_response("NOT_FOUND", "Page not found", 404)

        # Reorder remaining pages
        remaining_pages = PPTPage.query\
            .filter(PPTPage.project_id == project_id, PPTPage.id != page_id)\
            .order_by(PPTPage.order_index)\
            .all()

        for i, p in enumerate(remaining_pages):
            p.order_index = i

        db.session.delete(page)
        project.updated_at = datetime.utcnow()
        db.session.commit()

        return success_response(message="Page deleted successfully")

    except Exception as e:
        db.session.rollback()
        logger.error(f"delete_page failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_page_bp.route("/<project_id>/pages/<page_id>/versions", methods=["GET"])
@jwt_required()
def list_page_versions(project_id: str, page_id: str):
    """
    GET /api/ppt/projects/{project_id}/pages/{page_id}/versions - List image versions
    """
    try:
        user_id = get_jwt_identity()

        project, error = _verify_project_access(project_id, user_id)
        if error:
            return error

        page = PPTPage.query.filter_by(
            id=page_id,
            project_id=project_id
        ).first()

        if not page:
            return error_response("NOT_FOUND", "Page not found", 404)

        versions = PPTPageImageVersion.query\
            .filter_by(page_id=page_id)\
            .order_by(PPTPageImageVersion.version_number.desc())\
            .all()

        return success_response({
            "versions": [v.to_dict() for v in versions]
        })

    except Exception as e:
        logger.error(f"list_page_versions failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_page_bp.route("/<project_id>/pages/<page_id>/versions/<version_id>/activate", methods=["POST"])
@jwt_required()
def activate_version(project_id: str, page_id: str, version_id: str):
    """
    POST /api/ppt/projects/{project_id}/pages/{page_id}/versions/{version_id}/activate
    Set a specific version as the current image
    """
    try:
        user_id = get_jwt_identity()

        project, error = _verify_project_access(project_id, user_id)
        if error:
            return error

        page = PPTPage.query.filter_by(
            id=page_id,
            project_id=project_id
        ).first()

        if not page:
            return error_response("NOT_FOUND", "Page not found", 404)

        version = PPTPageImageVersion.query.filter_by(
            id=version_id,
            page_id=page_id
        ).first()

        if not version:
            return error_response("NOT_FOUND", "Version not found", 404)

        # Set all versions to not current
        PPTPageImageVersion.query\
            .filter_by(page_id=page_id)\
            .update({"is_current": False})

        # Set selected version as current
        version.is_current = True

        # Update page's generated_image_path
        page.generated_image_path = version.image_path
        page.updated_at = datetime.utcnow()
        project.updated_at = datetime.utcnow()

        db.session.commit()

        return success_response({
            "page": page.to_dict(),
            "activated_version": version.to_dict()
        })

    except Exception as e:
        db.session.rollback()
        logger.error(f"activate_version failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_page_bp.route("/<project_id>/pages/<page_id>/edit-image", methods=["POST"])
@jwt_required()
def edit_page_image(project_id: str, page_id: str):
    """
    POST /api/ppt/projects/{project_id}/pages/{page_id}/edit-image
    Edit a page's image using natural language instructions.

    Request body:
    {
        "instruction": "Make the title larger and add more contrast",
        "selection_mask": "base64_encoded_mask_image",  // Optional: region selection
        "material_images": ["base64_image1", "base64_image2"]  // Optional: new materials
    }

    Returns:
    {
        "success": true,
        "data": {
            "page": {...},
            "new_version": {...}
        }
    }
    """
    try:
        user_id = get_jwt_identity()

        project, error = _verify_project_access(project_id, user_id)
        if error:
            return error

        page = PPTPage.query.filter_by(
            id=page_id,
            project_id=project_id
        ).first()

        if not page:
            return error_response("NOT_FOUND", "Page not found", 404)

        if not page.generated_image_path:
            return error_response(
                "NO_IMAGE",
                "Page has no generated image to edit",
                400
            )

        data = request.get_json()
        instruction = data.get("instruction")

        if not instruction:
            return error_response(
                "INVALID_REQUEST",
                "Edit instruction is required",
                400
            )

        # Get the current image
        upload_folder = Path(current_app.config.get("UPLOAD_FOLDER", "uploads"))
        image_path = page.generated_image_path
        if image_path.startswith('/'):
            image_path = image_path.lstrip('/')

        full_image_path = upload_folder / image_path
        if not full_image_path.exists():
            # Try alternative path
            full_image_path = upload_folder / "ppt" / project_id / "pages" / os.path.basename(image_path)

        if not full_image_path.exists():
            return error_response(
                "IMAGE_NOT_FOUND",
                "Original image file not found",
                404
            )

        # Read original image
        with open(full_image_path, 'rb') as f:
            original_image_bytes = f.read()

        # Get optional parameters
        selection_mask = data.get("selection_mask")
        material_images_b64 = data.get("material_images", [])

        # Decode selection mask if provided
        selection_mask_bytes = None
        if selection_mask:
            try:
                selection_mask_bytes = base64.b64decode(selection_mask)
            except Exception as e:
                logger.warning(f"Failed to decode selection mask: {e}")

        # Decode material images if provided
        material_images = []
        for img_b64 in material_images_b64[:3]:  # Limit to 3 materials
            try:
                material_images.append(base64.b64decode(img_b64))
            except Exception as e:
                logger.warning(f"Failed to decode material image: {e}")

        # Get AI provider
        from backend.services.ppt.file_service import get_ai_provider_for_user
        ai_provider = get_ai_provider_for_user(user_id)

        if not ai_provider:
            return error_response(
                "NO_AI_PROVIDER",
                "AI provider not configured. Please configure your AI settings.",
                400
            )

        # Build prompt
        from backend.services.ppt.prompts import get_image_edit_with_region_prompt

        # Get page description for context
        description_content = page.get_description_content() if hasattr(page, 'get_description_content') else None
        original_description = None
        if description_content:
            original_description = description_content.get('text', '')

        prompt = get_image_edit_with_region_prompt(
            edit_instruction=instruction,
            original_description=original_description,
            has_selection_mask=selection_mask_bytes is not None,
            has_new_material=len(material_images) > 0
        )

        # Prepare reference images
        reference_images = [original_image_bytes]
        if selection_mask_bytes:
            reference_images.append(selection_mask_bytes)
        reference_images.extend(material_images)

        # Generate edited image
        result = ai_provider.generate_image(
            prompt=prompt,
            reference_images=reference_images
        )

        if not result.success:
            return error_response(
                "GENERATION_FAILED",
                f"Image edit failed: {result.error}",
                500
            )

        if not result.image_data:
            return error_response(
                "NO_IMAGE_DATA",
                "No image data in AI response",
                500
            )

        # Save the new version
        # Get next version number
        max_version = db.session.query(func.max(PPTPageImageVersion.version_number))\
            .filter_by(page_id=page_id).scalar() or 0
        next_version = max_version + 1

        # Save image file
        pages_dir = upload_folder / "ppt" / project_id / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)

        new_filename = f"page_{page.order_index}_v{next_version}.png"
        new_image_path = pages_dir / new_filename

        with open(new_image_path, 'wb') as f:
            f.write(result.image_data)

        relative_path = f"/ppt/{project_id}/pages/{new_filename}"

        # Mark old versions as not current
        PPTPageImageVersion.query.filter_by(page_id=page_id).update({"is_current": False})

        # Create new version record
        new_version = PPTPageImageVersion(
            page_id=page_id,
            image_path=relative_path,
            version_number=next_version,
            is_current=True
        )
        db.session.add(new_version)

        # Update page's current image
        page.generated_image_path = relative_path
        page.updated_at = datetime.utcnow()
        project.updated_at = datetime.utcnow()

        db.session.commit()

        return success_response({
            "page": page.to_dict(),
            "new_version": new_version.to_dict()
        })

    except Exception as e:
        db.session.rollback()
        logger.error(f"edit_page_image failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_page_bp.route("/<project_id>/pages/reorder", methods=["POST"])
@jwt_required()
def reorder_pages(project_id: str):
    """
    POST /api/ppt/projects/{project_id}/pages/reorder
    Reorder pages within a project.

    Request body:
    {
        "page_ids": ["id1", "id2", "id3"]  // New order of page IDs
    }
    """
    try:
        user_id = get_jwt_identity()

        project, error = _verify_project_access(project_id, user_id)
        if error:
            return error

        data = request.get_json()
        page_ids = data.get("page_ids", [])

        if not page_ids:
            return error_response(
                "INVALID_REQUEST",
                "page_ids array is required",
                400
            )

        # Verify all page IDs belong to this project
        pages = PPTPage.query.filter(
            PPTPage.project_id == project_id,
            PPTPage.id.in_(page_ids)
        ).all()

        if len(pages) != len(page_ids):
            return error_response(
                "INVALID_PAGE_IDS",
                "Some page IDs are invalid or don't belong to this project",
                400
            )

        # Create a map of id -> page
        page_map = {p.id: p for p in pages}

        # Update order based on new sequence
        for i, page_id in enumerate(page_ids):
            if page_id in page_map:
                page_map[page_id].order_index = i

        project.updated_at = datetime.utcnow()
        db.session.commit()

        # Return updated pages in new order
        updated_pages = PPTPage.query\
            .filter_by(project_id=project_id)\
            .order_by(PPTPage.order_index)\
            .all()

        return success_response({
            "pages": [p.to_dict() for p in updated_pages]
        })

    except Exception as e:
        db.session.rollback()
        logger.error(f"reorder_pages failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)
