"""
PPT Project routes - handles project-related endpoints with JWT authentication
"""
import logging
from datetime import datetime

from flask import Blueprint, request
from flask_jwt_extended import get_jwt_identity, jwt_required
from sqlalchemy import desc
from sqlalchemy.orm import joinedload

from backend.extensions import db
from backend.models.ppt import PPTPage, PPTProject, PPTTask
from backend.services import pricing
from backend.services.credit_service import charge, check_sufficient_credits
from backend.utils.response import error_response, success_response

logger = logging.getLogger(__name__)

ppt_project_bp = Blueprint("ppt_projects", __name__)


@ppt_project_bp.route("", methods=["GET"])
@jwt_required()
def list_projects():
    """
    GET /api/ppt/projects - List user's PPT projects

    Query params:
    - limit: number of projects (default: 50, max: 100)
    - offset: offset for pagination (default: 0)
    - team_id: filter by team (optional)
    """
    try:
        user_id = get_jwt_identity()

        limit = request.args.get("limit", 50, type=int)
        offset = request.args.get("offset", 0, type=int)
        team_id = request.args.get("team_id")

        limit = min(max(1, limit), 100)
        offset = max(0, offset)

        query = PPTProject.query.options(joinedload(PPTProject.pages))

        # Filter by user or team
        if team_id:
            query = query.filter(PPTProject.team_id == team_id)
        else:
            query = query.filter(PPTProject.user_id == user_id)

        query = query.order_by(desc(PPTProject.updated_at))

        # Fetch limit + 1 to check for more pages
        projects_with_extra = query.limit(limit + 1).offset(offset).all()

        has_more = len(projects_with_extra) > limit
        projects = projects_with_extra[:limit]

        return success_response({
            "projects": [p.to_dict(include_pages=True) for p in projects],
            "has_more": has_more,
            "limit": limit,
            "offset": offset
        })

    except Exception as e:
        logger.error(f"list_projects failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_project_bp.route("", methods=["POST"])
@jwt_required()
def create_project():
    """
    POST /api/ppt/projects - Create a new PPT project

    Request body:
    {
        "creation_type": "idea|outline|descriptions",
        "idea_prompt": "...",
        "outline_text": "...",
        "description_text": "...",
        "template_style": "...",
        "team_id": "optional"
    }
    """
    try:
        user_id = get_jwt_identity()
        data = request.get_json()

        if not data:
            return error_response("BAD_REQUEST", "Request body is required", 400)

        creation_type = data.get("creation_type", "idea")
        if creation_type not in ["idea", "outline", "descriptions"]:
            return error_response("BAD_REQUEST", "Invalid creation_type", 400)

        project = PPTProject(
            user_id=user_id,
            team_id=data.get("team_id"),
            creation_type=creation_type,
            idea_prompt=data.get("idea_prompt"),
            outline_text=data.get("outline_text"),
            description_text=data.get("description_text"),
            template_style=data.get("template_style"),
            status="DRAFT"
        )

        db.session.add(project)
        db.session.commit()

        return success_response({
            "project_id": project.id,
            "status": project.status,
            "pages": []
        }, status_code=201)

    except Exception as e:
        db.session.rollback()
        logger.error(f"create_project failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_project_bp.route("/<project_id>", methods=["GET"])
@jwt_required()
def get_project(project_id: str):
    """
    GET /api/ppt/projects/{project_id} - Get project details
    """
    try:
        user_id = get_jwt_identity()

        project = PPTProject.query\
            .options(joinedload(PPTProject.pages))\
            .filter(PPTProject.id == project_id)\
            .first()

        if not project:
            return error_response("NOT_FOUND", "Project not found", 404)

        # Check ownership: user must be owner or team member
        if project.user_id != user_id:
            if not project.team_id:
                return error_response("FORBIDDEN", "Access denied", 403)
            # Check if user is a team member
            from backend.models.team import TeamMember
            membership = TeamMember.query.filter_by(
                team_id=project.team_id, user_id=user_id
            ).first()
            if not membership:
                return error_response("FORBIDDEN", "Access denied", 403)

        return success_response(project.to_dict(include_pages=True))

    except Exception as e:
        logger.error(f"get_project failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_project_bp.route("/<project_id>", methods=["PUT"])
@jwt_required()
def update_project(project_id: str):
    """
    PUT /api/ppt/projects/{project_id} - Update project

    Request body:
    {
        "idea_prompt": "...",
        "extra_requirements": "...",
        "template_style": "...",
        "export_extractor_method": "...",
        "export_inpaint_method": "...",
        "pages_order": ["page-id-1", "page-id-2", ...]
    }
    """
    try:
        user_id = get_jwt_identity()

        project = PPTProject.query\
            .options(joinedload(PPTProject.pages))\
            .filter(PPTProject.id == project_id)\
            .first()

        if not project:
            return error_response("NOT_FOUND", "Project not found", 404)

        if project.user_id != user_id:
            return error_response("FORBIDDEN", "Access denied", 403)

        data = request.get_json()

        # Update fields if provided
        if "idea_prompt" in data:
            project.idea_prompt = data["idea_prompt"]
        if "extra_requirements" in data:
            project.extra_requirements = data["extra_requirements"]
        if "template_style" in data:
            project.template_style = data["template_style"]
        if "export_extractor_method" in data:
            project.export_extractor_method = data["export_extractor_method"]
        if "export_inpaint_method" in data:
            project.export_inpaint_method = data["export_inpaint_method"]

        # Update page order if provided
        if "pages_order" in data:
            pages_order = data["pages_order"]
            pages_to_update = PPTPage.query.filter(
                PPTPage.id.in_(pages_order),
                PPTPage.project_id == project_id
            ).all()

            pages_map = {page.id: page for page in pages_to_update}
            for index, page_id in enumerate(pages_order):
                if page_id in pages_map:
                    pages_map[page_id].order_index = index

        project.updated_at = datetime.utcnow()
        db.session.commit()

        return success_response(project.to_dict(include_pages=True))

    except Exception as e:
        db.session.rollback()
        logger.error(f"update_project failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_project_bp.route("/<project_id>", methods=["DELETE"])
@jwt_required()
def delete_project(project_id: str):
    """
    DELETE /api/ppt/projects/{project_id} - Delete project
    """
    try:
        user_id = get_jwt_identity()

        project = PPTProject.query.get(project_id)

        if not project:
            return error_response("NOT_FOUND", "Project not found", 404)

        if project.user_id != user_id:
            return error_response("FORBIDDEN", "Access denied", 403)

        # TODO: Delete project files from storage

        db.session.delete(project)
        db.session.commit()

        return success_response(message="Project deleted successfully")

    except Exception as e:
        db.session.rollback()
        logger.error(f"delete_project failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_project_bp.route("/<project_id>/tasks/<task_id>", methods=["GET"])
@jwt_required()
def get_task_status(project_id: str, task_id: str):
    """
    GET /api/ppt/projects/{project_id}/tasks/{task_id} - Get task status
    """
    try:
        user_id = get_jwt_identity()

        # Verify project ownership
        project = PPTProject.query.get(project_id)
        if not project:
            return error_response("NOT_FOUND", "Project not found", 404)

        if project.user_id != user_id:
            return error_response("FORBIDDEN", "Access denied", 403)

        task = PPTTask.query.get(task_id)
        if not task or task.project_id != project_id:
            return error_response("NOT_FOUND", "Task not found", 404)

        return success_response(task.to_dict())

    except Exception as e:
        logger.error(f"get_task_status failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_project_bp.route("/<project_id>/tasks", methods=["GET"])
@jwt_required()
def list_project_tasks(project_id: str):
    """
    GET /api/ppt/projects/{project_id}/tasks - List all tasks for a project

    Query params:
    - status: Filter by status (PENDING, COMPLETED, FAILED)
    - task_type: Filter by task type (GENERATE_DESCRIPTIONS, GENERATE_IMAGES, EXPORT)
    - limit: Number of tasks to return (default: 50)
    - offset: Offset for pagination (default: 0)

    Returns:
        List of tasks with pagination info
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

        # Build query
        query = PPTTask.query.filter_by(project_id=project_id)

        # Apply filters
        status = request.args.get("status")
        if status:
            query = query.filter(PPTTask.status == status)

        task_type = request.args.get("task_type")
        if task_type:
            query = query.filter(PPTTask.task_type == task_type)

        # Pagination
        limit = request.args.get("limit", 50, type=int)
        offset = request.args.get("offset", 0, type=int)
        limit = min(max(1, limit), 100)
        offset = max(0, offset)

        # Order by created_at descending (most recent first)
        query = query.order_by(desc(PPTTask.created_at))

        # Fetch with pagination
        tasks_with_extra = query.limit(limit + 1).offset(offset).all()
        has_more = len(tasks_with_extra) > limit
        tasks = tasks_with_extra[:limit]

        return success_response({
            "tasks": [t.to_dict() for t in tasks],
            "has_more": has_more,
            "limit": limit,
            "offset": offset
        })

    except Exception as e:
        logger.error(f"list_project_tasks failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_project_bp.route("/tasks", methods=["GET"])
@jwt_required()
def list_user_tasks():
    """
    GET /api/ppt/tasks - List all tasks for current user across all projects

    Query params:
    - status: Filter by status (PENDING, COMPLETED, FAILED)
    - task_type: Filter by task type (GENERATE_DESCRIPTIONS, GENERATE_IMAGES, EXPORT)
    - limit: Number of tasks to return (default: 50)
    - offset: Offset for pagination (default: 0)

    Returns:
        List of tasks with pagination info
    """
    try:
        user_id = get_jwt_identity()

        # Get user's project IDs
        user_projects = PPTProject.query.filter_by(user_id=user_id).with_entities(PPTProject.id).all()
        project_ids = [p.id for p in user_projects]

        if not project_ids:
            return success_response({
                "tasks": [],
                "has_more": False,
                "limit": 50,
                "offset": 0
            })

        # Build query
        query = PPTTask.query.filter(PPTTask.project_id.in_(project_ids))

        # Apply filters
        status = request.args.get("status")
        if status:
            query = query.filter(PPTTask.status == status)

        task_type = request.args.get("task_type")
        if task_type:
            query = query.filter(PPTTask.task_type == task_type)

        # Pagination
        limit = request.args.get("limit", 50, type=int)
        offset = request.args.get("offset", 0, type=int)
        limit = min(max(1, limit), 100)
        offset = max(0, offset)

        # Order by created_at descending (most recent first)
        query = query.order_by(desc(PPTTask.created_at))

        # Fetch with pagination
        tasks_with_extra = query.limit(limit + 1).offset(offset).all()
        has_more = len(tasks_with_extra) > limit
        tasks = tasks_with_extra[:limit]

        return success_response({
            "tasks": [t.to_dict() for t in tasks],
            "has_more": has_more,
            "limit": limit,
            "offset": offset
        })

    except Exception as e:
        logger.error(f"list_user_tasks failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


# AI Generation Endpoints

@ppt_project_bp.route("/<project_id>/generate/outline", methods=["POST"])
@jwt_required()
def generate_outline(project_id: str):
    """
    POST /api/ppt/projects/{project_id}/generate/outline - Generate outline (synchronous)

    Request body:
    {
        "language": "zh|en|ja|auto"  (optional, default: "zh")
    }
    """

    from backend.services.ai.factory import get_text_provider
    from backend.services.ppt.generation_service import (
        generate_outline_from_idea,
        parse_outline_text,
    )

    try:
        user_id = get_jwt_identity()

        # Verify project ownership
        project = PPTProject.query.options(joinedload(PPTProject.pages)).filter(
            PPTProject.id == project_id
        ).first()

        if not project:
            return error_response("NOT_FOUND", "Project not found", 404)
        if project.user_id != user_id:
            return error_response("FORBIDDEN", "Access denied", 403)

        data = request.get_json() or {}
        language = data.get("language", "zh")

        # Get AI provider
        ai_provider = get_text_provider()
        if not ai_provider or not ai_provider.is_configured():
            return error_response("SERVICE_UNAVAILABLE", "AI service not configured", 503)

        # Credit pre-check before spending the AI call. Authoritative deduction
        # happens atomically on success below. Team projects bill the team balance.
        team_id = project.team_id
        if not check_sufficient_credits(user_id, pricing.PPT_OUTLINE, team_id):
            return error_response("INSUFFICIENT_CREDITS", "积分不足", 402)

        # Generate outline based on creation_type
        if project.creation_type == "idea":
            if not project.idea_prompt:
                return error_response("BAD_REQUEST", "idea_prompt is required for idea type", 400)
            pages_data = generate_outline_from_idea(
                project.idea_prompt,
                ai_provider,
                language=language
            )
        elif project.creation_type == "outline":
            if not project.outline_text:
                return error_response("BAD_REQUEST", "outline_text is required for outline type", 400)
            pages_data = parse_outline_text(
                project.outline_text,
                ai_provider,
                language=language
            )
        else:
            return error_response("BAD_REQUEST", f"Unsupported creation_type: {project.creation_type}", 400)

        # Delete existing pages (cascade will handle image versions)
        PPTPage.query.filter_by(project_id=project_id).delete()

        # Create new pages
        new_pages = []
        for i, page_data in enumerate(pages_data):
            page = PPTPage(
                project_id=project_id,
                order_index=i,
                part=page_data.get("part"),
                status="DRAFT"
            )
            page.set_outline_content({
                "title": page_data.get("title", "Untitled"),
                "points": page_data.get("points", [])
            })
            db.session.add(page)
            new_pages.append(page)

        # Update project status
        project.status = "OUTLINE_GENERATED"
        project.updated_at = datetime.utcnow()

        # Charge for the generated outline. deduct_credits commits the pending
        # pages + status change together with the deduction in one transaction.
        charge(user_id, pricing.PPT_OUTLINE, "ppt_outline", "ppt_project", project_id,
               description="PPT 大纲生成", team_id=team_id)

        db.session.commit()

        return success_response({
            "project_id": project.id,
            "status": project.status,
            "pages": [page.to_dict() for page in new_pages]
        })

    except Exception as e:
        db.session.rollback()
        logger.error(f"generate_outline failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_project_bp.route("/<project_id>/generate/descriptions", methods=["POST"])
@jwt_required()
def generate_descriptions(project_id: str):
    """
    POST /api/ppt/projects/{project_id}/generate/descriptions - Generate descriptions (async)

    Request body:
    {
        "language": "zh|en|ja|auto"  (optional, default: "zh")
    }

    Returns 202 with task_id for polling
    """
    from flask import current_app

    from backend.services.ppt.task_manager import generate_descriptions_task, ppt_task_manager

    try:
        user_id = get_jwt_identity()

        # Verify project ownership
        project = PPTProject.query.options(joinedload(PPTProject.pages)).filter(
            PPTProject.id == project_id
        ).first()

        if not project:
            return error_response("NOT_FOUND", "Project not found", 404)
        if project.user_id != user_id:
            return error_response("FORBIDDEN", "Access denied", 403)

        pages = list(project.pages)
        if not pages:
            return error_response("BAD_REQUEST", "No pages to generate descriptions for", 400)

        # Credit pre-flight: block only if the payer cannot afford even one page.
        # The background task bills per-success and stops gracefully when exhausted.
        if not check_sufficient_credits(user_id, pricing.PPT_DESCRIPTION_PAGE, project.team_id):
            return error_response("INSUFFICIENT_CREDITS", "积分不足", 402)

        data = request.get_json() or {}
        language = data.get("language", "zh")

        # Create task record
        task = PPTTask(
            project_id=project_id,
            task_type="GENERATE_DESCRIPTIONS",
            status="PENDING"
        )
        task.set_progress({"total": len(pages), "completed": 0, "failed": 0})
        db.session.add(task)
        db.session.commit()

        # Submit background task
        app = current_app._get_current_object()
        ppt_task_manager.submit_task(
            task.id,
            generate_descriptions_task,
            app,
            project_id,
            language
        )

        return success_response({
            "task_id": task.id,
            "status": task.status
        }, status_code=202)

    except Exception as e:
        db.session.rollback()
        logger.error(f"generate_descriptions failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_project_bp.route("/<project_id>/generate/images", methods=["POST"])
@jwt_required()
def generate_images(project_id: str):
    """
    POST /api/ppt/projects/{project_id}/generate/images - Generate images (async)

    Request body:
    {
        "language": "zh|en|ja|auto"  (optional, default: "zh"),
        "page_ids": ["page-id-1", ...]  (optional, generate specific pages only)
    }

    Returns 202 with task_id for polling
    """
    from flask import current_app

    from backend.services.ppt.task_manager import generate_images_task, ppt_task_manager

    try:
        user_id = get_jwt_identity()

        # Verify project ownership
        project = PPTProject.query.options(joinedload(PPTProject.pages)).filter(
            PPTProject.id == project_id
        ).first()

        if not project:
            return error_response("NOT_FOUND", "Project not found", 404)
        if project.user_id != user_id:
            return error_response("FORBIDDEN", "Access denied", 403)

        data = request.get_json() or {}
        language = data.get("language", "zh")
        page_ids = data.get("page_ids")

        # Get pages to generate
        if page_ids:
            pages = [p for p in project.pages if p.id in page_ids]
        else:
            pages = list(project.pages)

        if not pages:
            return error_response("BAD_REQUEST", "No pages to generate images for", 400)

        # Credit pre-flight: block only if the payer cannot afford even one image.
        # The background task bills per-success and stops gracefully when exhausted.
        if not check_sufficient_credits(user_id, pricing.PPT_IMAGE_PAGE, project.team_id):
            return error_response("INSUFFICIENT_CREDITS", "积分不足", 402)

        # Create task record
        task = PPTTask(
            project_id=project_id,
            task_type="GENERATE_IMAGES",
            status="PENDING"
        )
        task.set_progress({"total": len(pages), "completed": 0, "failed": 0})
        db.session.add(task)
        db.session.commit()

        # Submit background task
        app = current_app._get_current_object()
        ppt_task_manager.submit_task(
            task.id,
            generate_images_task,
            app,
            project_id,
            page_ids,
            language
        )

        return success_response({
            "task_id": task.id,
            "status": task.status
        }, status_code=202)

    except Exception as e:
        db.session.rollback()
        logger.error(f"generate_images failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_project_bp.route("/<project_id>/refine/outline", methods=["POST"])
@jwt_required()
def refine_outline(project_id: str):
    """
    POST /api/ppt/projects/{project_id}/refine/outline - Refine outline

    TODO: Implement AI service integration
    """
    return error_response("NOT_IMPLEMENTED", "AI refinement not yet implemented", 501)


@ppt_project_bp.route("/<project_id>/refine/descriptions", methods=["POST"])
@jwt_required()
def refine_descriptions(project_id: str):
    """
    POST /api/ppt/projects/{project_id}/refine/descriptions - Refine descriptions

    TODO: Implement AI service integration
    """
    return error_response("NOT_IMPLEMENTED", "AI refinement not yet implemented", 501)
