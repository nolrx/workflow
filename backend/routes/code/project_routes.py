"""
Code creation routes.
"""
from flask import Blueprint, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from backend.extensions import db
from backend.models.code import CodeDocument, CodeProject, CodeProjectStatus
from backend.services.code import get_code_generation_service, list_styles
from backend.services.prompt_library import (
    PROMPT_RECIPE_EXAMPLES,
    PROMPT_RECIPES,
    SYSTEM_PROMPT_ASSEMBLY_GUIDE,
    compose_system_prompt,
    get_prefix,
    list_prefixes,
    route_prefixes,
)
from backend.utils.response import error_response, success_response

code_project_bp = Blueprint("code_project", __name__)


@code_project_bp.route("/styles", methods=["GET"])
@jwt_required()
def get_ui_styles():
    """Return available UI styles."""
    return success_response({"styles": list_styles()})


@code_project_bp.route("/prompt-prefixes", methods=["GET"])
@jwt_required()
def get_prompt_prefixes():
    """Return available role prompt prefixes."""
    include_text = request.args.get("include_text") in ("1", "true", "yes")
    return success_response(
        {
            "prefixes": list_prefixes(include_text=include_text),
            "recipes": {key: list(value) for key, value in PROMPT_RECIPES.items()},
            "recipe_examples": PROMPT_RECIPE_EXAMPLES,
            "assembly_guide": SYSTEM_PROMPT_ASSEMBLY_GUIDE,
        }
    )


@code_project_bp.route("/prompt-prefixes/<prefix_id>", methods=["GET"])
@jwt_required()
def get_prompt_prefix(prefix_id: str):
    """Return one role prompt prefix."""
    try:
        prefix = get_prefix(prefix_id)
    except ValueError:
        return error_response("NOT_FOUND", "提示词前缀不存在", 404)
    return success_response({"prefix": prefix.to_dict(include_text=True)})


@code_project_bp.route("/prompt-prefixes/route", methods=["POST"])
@jwt_required()
def route_prompt_prefixes():
    """Route a task to role prompt prefixes."""
    data = request.get_json() or {}
    task = (data.get("task") or data.get("requirement") or "").strip()
    if not task:
        return error_response("VALIDATION_ERROR", "任务内容不能为空", 400)
    route = route_prefixes(task)
    return success_response({"route": route.to_dict()})


@code_project_bp.route("/prompt-prefixes/compose", methods=["POST"])
@jwt_required()
def compose_prompt_prefixes():
    """Compose base, role prefixes, and output contract into one system prompt."""
    data = request.get_json() or {}
    primary_role = (data.get("primary_role") or "").strip()
    if not primary_role:
        return error_response("VALIDATION_ERROR", "主责角色不能为空", 400)
    secondary_roles = data.get("secondary_roles") or []
    if not isinstance(secondary_roles, list):
        return error_response("VALIDATION_ERROR", "协作角色必须是数组", 400)
    try:
        system_prompt = compose_system_prompt(
            primary_role=primary_role,
            secondary_roles=secondary_roles,
            include_base=bool(data.get("include_base", True)),
            include_output_contract=bool(data.get("include_output_contract", True)),
        )
    except ValueError as error:
        return error_response("VALIDATION_ERROR", str(error), 400)
    return success_response({"system_prompt": system_prompt})


@code_project_bp.route("/projects", methods=["GET"])
@jwt_required()
def list_projects():
    """List current user's code creation projects."""
    user_id = get_jwt_identity()
    limit = min(int(request.args.get("limit", 50)), 100)
    offset = int(request.args.get("offset", 0))
    query = CodeProject.query.filter_by(user_id=user_id).order_by(CodeProject.updated_at.desc())
    projects = query.limit(limit).offset(offset).all()
    return success_response(
        {
            "projects": [project.to_list_dict() for project in projects],
            "has_more": query.count() > offset + len(projects),
            "limit": limit,
            "offset": offset,
        }
    )


@code_project_bp.route("/projects", methods=["POST"])
@jwt_required()
def create_project():
    """Create a software creation project and generate the requirements document."""
    user_id = get_jwt_identity()
    data = request.get_json() or {}
    requirement = (data.get("requirement") or "").strip()
    if not requirement:
        return error_response("VALIDATION_ERROR", "需求不能为空", 400)

    title = (data.get("title") or requirement[:60]).strip()
    service = get_code_generation_service()
    requirements_doc = service.generate_requirements(requirement)
    project = CodeProject(
        user_id=user_id,
        team_id=data.get("team_id"),
        title=title,
        requirement_input=requirement,
        requirements_doc=requirements_doc,
        status=CodeProjectStatus.REQUIREMENT_READY,
    )
    db.session.add(project)
    db.session.commit()
    return success_response({"project": project.to_dict()}, "项目已创建", 201)


@code_project_bp.route("/projects/<project_id>", methods=["GET"])
@jwt_required()
def get_project(project_id: str):
    """Return a project."""
    project = _get_owned_project(project_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在", 404)
    return success_response({"project": project.to_dict()})


@code_project_bp.route("/projects/<project_id>", methods=["PATCH"])
@jwt_required()
def update_project(project_id: str):
    """Update editable project-level fields."""
    project = _get_owned_project(project_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在", 404)

    data = request.get_json() or {}
    for field in (
        "title",
        "requirement_input",
        "requirements_doc",
        "development_flow",
        "style_prompt",
        "ui_baseline_prompt",
        "confirmed_preview_url",
    ):
        if field in data:
            setattr(project, field, data.get(field))
    db.session.commit()
    return success_response({"project": project.to_dict()}, "项目已保存")


@code_project_bp.route("/projects/<project_id>/flow", methods=["POST"])
@jwt_required()
def generate_flow(project_id: str):
    """Generate the software development process from the requirements document."""
    project = _get_owned_project(project_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在", 404)
    if not project.requirements_doc:
        return error_response("VALIDATION_ERROR", "请先生成或填写需求文档", 400)

    service = get_code_generation_service()
    project.development_flow = service.generate_development_flow(project.requirements_doc)
    project.status = CodeProjectStatus.FLOW_READY
    db.session.commit()
    return success_response({"project": project.to_dict()}, "开发流程已生成")


@code_project_bp.route("/projects/<project_id>/documents", methods=["POST"])
@jwt_required()
def split_documents(project_id: str):
    """Split project artifacts into editable development documents."""
    project = _get_owned_project(project_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在", 404)
    if not project.requirements_doc or not project.development_flow:
        return error_response("VALIDATION_ERROR", "请先完成需求文档和开发流程", 400)

    service = get_code_generation_service()
    documents = service.split_documents(project.requirements_doc, project.development_flow)
    project.documents.delete()
    for document in documents:
        db.session.add(
            CodeDocument(
                project_id=project.id,
                document_type=document["document_type"],
                title=document["title"],
                content=document["content"],
                prompt_expert=document["prompt_expert"],
                order_index=document["order_index"],
            )
        )
    project.status = CodeProjectStatus.DOCUMENTS_READY
    db.session.commit()
    return success_response({"project": project.to_dict()}, "开发文档已切分")


@code_project_bp.route("/projects/<project_id>/documents/<document_id>", methods=["PATCH"])
@jwt_required()
def update_document(project_id: str, document_id: str):
    """Update an editable development document."""
    project = _get_owned_project(project_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在", 404)
    document = CodeDocument.query.filter_by(id=document_id, project_id=project.id).first()
    if not document:
        return error_response("NOT_FOUND", "文档不存在", 404)

    data = request.get_json() or {}
    for field in ("title", "content", "prompt_expert", "document_type", "order_index"):
        if field in data:
            setattr(document, field, data.get(field))
    db.session.commit()
    return success_response({"document": document.to_dict()}, "文档已保存")


@code_project_bp.route("/projects/<project_id>/style-prompt", methods=["POST"])
@jwt_required()
def generate_style_prompt(project_id: str):
    """Generate a style-specific document and prompt."""
    project = _get_owned_project(project_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在", 404)
    data = request.get_json() or {}
    style_ids = data.get("style_ids") or []
    if not isinstance(style_ids, list) or not style_ids:
        return error_response("VALIDATION_ERROR", "请至少选择一种应用风格", 400)

    service = get_code_generation_service()
    project.set_selected_style_ids(style_ids)
    project.style_prompt = service.generate_style_prompt(project.requirement_input, style_ids)
    project.status = CodeProjectStatus.STYLE_READY
    db.session.commit()
    return success_response({"project": project.to_dict()}, "风格文档已生成")


@code_project_bp.route("/projects/<project_id>/previews", methods=["POST"])
@jwt_required()
def generate_previews(project_id: str):
    """Generate application preview thumbnails."""
    project = _get_owned_project(project_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在", 404)

    data = request.get_json() or {}
    prompt = (data.get("prompt") or project.style_prompt or "").strip()
    if not prompt:
        return error_response("VALIDATION_ERROR", "请先生成风格提示词", 400)

    service = get_code_generation_service()
    try:
        images = service.generate_preview_images(prompt, count=int(data.get("count") or 2))
    except RuntimeError as error:
        return error_response("PREVIEW_IMAGE_FAILED", str(error), 502)
    project.set_preview_images(images)
    project.status = CodeProjectStatus.PREVIEW_READY
    db.session.commit()
    return success_response({"project": project.to_dict()}, "应用缩略图已生成")


@code_project_bp.route("/projects/<project_id>/confirm-preview", methods=["POST"])
@jwt_required()
def confirm_preview(project_id: str):
    """Confirm a preview as the UI baseline."""
    project = _get_owned_project(project_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在", 404)
    data = request.get_json() or {}
    preview_url = data.get("preview_url")
    if not preview_url:
        return error_response("VALIDATION_ERROR", "请选择一个缩略图", 400)

    project.confirmed_preview_url = preview_url
    project.ui_baseline_prompt = data.get("ui_baseline_prompt") or project.style_prompt
    project.status = CodeProjectStatus.UI_CONFIRMED
    db.session.commit()
    return success_response({"project": project.to_dict()}, "UI 基调已确认")


def _get_owned_project(project_id: str) -> CodeProject | None:
    user_id = get_jwt_identity()
    return CodeProject.query.filter_by(id=project_id, user_id=user_id).first()
