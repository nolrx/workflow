"""
PPT File routes - serves generated images and templates
No authentication required for file access
"""
import os
import logging
from pathlib import Path
from flask import Blueprint, send_file, current_app
from werkzeug.utils import safe_join

logger = logging.getLogger(__name__)

ppt_file_bp = Blueprint("ppt_files", __name__)


def _get_upload_folder():
    """Get upload folder path"""
    return Path(current_app.config.get("UPLOAD_FOLDER", "uploads"))


@ppt_file_bp.route("/<project_id>/pages/<filename>")
def serve_page_image(project_id: str, filename: str):
    """
    GET /api/ppt/files/{project_id}/pages/{filename}
    Serve generated page images (no auth required)
    """
    try:
        upload_folder = _get_upload_folder()
        pages_dir = upload_folder / "ppt" / project_id / "pages"

        # Security: prevent path traversal
        filepath = safe_join(str(pages_dir), filename)
        if not filepath or not os.path.exists(filepath):
            return {"success": False, "error": "NOT_FOUND", "message": "File not found"}, 404

        # Determine mimetype
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'png'
        mimetype = f"image/{ext}" if ext in ['png', 'jpg', 'jpeg', 'gif', 'webp'] else 'application/octet-stream'

        return send_file(filepath, mimetype=mimetype)

    except Exception as e:
        logger.error(f"serve_page_image failed: {str(e)}", exc_info=True)
        return {"success": False, "error": "SERVER_ERROR", "message": str(e)}, 500


@ppt_file_bp.route("/<project_id>/template/<filename>")
def serve_template_image(project_id: str, filename: str):
    """
    GET /api/ppt/files/{project_id}/template/{filename}
    Serve template images (no auth required)
    """
    try:
        upload_folder = _get_upload_folder()
        template_dir = upload_folder / "ppt" / project_id / "template"

        # Security: prevent path traversal
        filepath = safe_join(str(template_dir), filename)
        if not filepath or not os.path.exists(filepath):
            return {"success": False, "error": "NOT_FOUND", "message": "File not found"}, 404

        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'png'
        mimetype = f"image/{ext}" if ext in ['png', 'jpg', 'jpeg', 'gif', 'webp'] else 'application/octet-stream'

        return send_file(filepath, mimetype=mimetype)

    except Exception as e:
        logger.error(f"serve_template_image failed: {str(e)}", exc_info=True)
        return {"success": False, "error": "SERVER_ERROR", "message": str(e)}, 500


@ppt_file_bp.route("/<project_id>/materials/<filename>")
def serve_material_image(project_id: str, filename: str):
    """
    GET /api/ppt/files/{project_id}/materials/{filename}
    Serve material images (no auth required)
    """
    try:
        upload_folder = _get_upload_folder()
        materials_dir = upload_folder / "ppt" / project_id / "materials"

        # Security: prevent path traversal
        filepath = safe_join(str(materials_dir), filename)
        if not filepath or not os.path.exists(filepath):
            return {"success": False, "error": "NOT_FOUND", "message": "File not found"}, 404

        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'png'
        mimetype = f"image/{ext}" if ext in ['png', 'jpg', 'jpeg', 'gif', 'webp'] else 'application/octet-stream'

        return send_file(filepath, mimetype=mimetype)

    except Exception as e:
        logger.error(f"serve_material_image failed: {str(e)}", exc_info=True)
        return {"success": False, "error": "SERVER_ERROR", "message": str(e)}, 500


@ppt_file_bp.route("/<project_id>/exports/<filename>")
def serve_export_file(project_id: str, filename: str):
    """
    GET /api/ppt/files/{project_id}/exports/{filename}
    Serve exported PPT/PDF files (no auth required)
    """
    try:
        upload_folder = _get_upload_folder()
        exports_dir = upload_folder / "ppt" / project_id / "exports"

        # Security: prevent path traversal
        filepath = safe_join(str(exports_dir), filename)
        if not filepath or not os.path.exists(filepath):
            return {"success": False, "error": "NOT_FOUND", "message": "File not found"}, 404

        # Determine mimetype based on extension
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
        mimetypes = {
            'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
            'pdf': 'application/pdf',
            'zip': 'application/zip'
        }
        mimetype = mimetypes.get(ext, 'application/octet-stream')

        return send_file(filepath, mimetype=mimetype, as_attachment=True, download_name=filename)

    except Exception as e:
        logger.error(f"serve_export_file failed: {str(e)}", exc_info=True)
        return {"success": False, "error": "SERVER_ERROR", "message": str(e)}, 500


@ppt_file_bp.route("/global/materials/<filename>")
def serve_global_material_image(filename: str):
    """
    GET /api/ppt/files/global/materials/{filename}
    Serve global material images (no auth required)
    """
    try:
        upload_folder = _get_upload_folder()
        materials_dir = upload_folder / "ppt" / "global" / "materials"

        # Security: prevent path traversal
        filepath = safe_join(str(materials_dir), filename)
        if not filepath or not os.path.exists(filepath):
            return {"success": False, "error": "NOT_FOUND", "message": "File not found"}, 404

        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'png'
        mimetype = f"image/{ext}" if ext in ['png', 'jpg', 'jpeg', 'gif', 'webp'] else 'application/octet-stream'

        return send_file(filepath, mimetype=mimetype)

    except Exception as e:
        logger.error(f"serve_global_material_image failed: {str(e)}", exc_info=True)
        return {"success": False, "error": "SERVER_ERROR", "message": str(e)}, 500
