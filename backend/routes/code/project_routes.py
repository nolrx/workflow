"""
Code creation routes.
"""
from flask import Blueprint, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from backend.extensions import db
from backend.models.agent.run import AgentRun
from backend.models.code import (
    CodeCanvas,
    CodeDeployment,
    CodeDocument,
    CodeProject,
    CodeProjectLedger,
    CodeProjectStatus,
    CodeStage,
    CodeStageVersionSource,
    DeploymentStatus,
)
from backend.services import pricing
from backend.services.agent.context_ledger import ContextLedger
from backend.services.code import get_code_generation_service, list_styles
from backend.services.code.version_service import (
    activate_stage_version,
    get_stage_version,
    list_stage_versions,
    record_versions_for_fields,
    safe_record_stage_version,
)
from backend.services.credit_service import charge, refund_credits
from backend.services.prompt_library import (
    PROMPT_RECIPE_EXAMPLES,
    PROMPT_RECIPES,
    SYSTEM_PROMPT_ASSEMBLY_GUIDE,
    compose_system_prompt,
    get_prefix,
    list_prefixes,
    resolve_prefix_text,
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
    data = prefix.to_dict(include_text=True)
    data["text"] = resolve_prefix_text(prefix_id)  # overlay admin edits
    return success_response({"prefix": data})


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
    project_ids = [project.id for project in projects]
    deployments = (
        CodeDeployment.query.filter(CodeDeployment.project_id.in_(project_ids)).all()
        if project_ids
        else []
    )
    deployment_status_map = {deployment.project_id: deployment.status for deployment in deployments}
    return success_response(
        {
            "projects": [
                project.to_list_dict(deployment_status=deployment_status_map.get(project.id))
                for project in projects
            ],
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
    safe_record_stage_version(project, CodeStage.REQUIREMENTS)
    return success_response({"project": project.to_dict()}, "项目已创建", 201)


@code_project_bp.route("/projects/<project_id>", methods=["GET"])
@jwt_required()
def get_project(project_id: str):
    """Return a project."""
    project = _get_owned_project(project_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在", 404)
    return success_response({"project": project.to_dict()})


@code_project_bp.route("/projects/<project_id>", methods=["DELETE"])
@jwt_required()
def delete_project(project_id: str):
    """Delete a code project and its associated runs/records.

    Refuses deletion when the project has an active deployment or a running
    agent run, to avoid orphaning live containers or in-flight work.
    """
    project = _get_owned_project(project_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在", 404)

    deployment = CodeDeployment.query.filter_by(project_id=project_id).first()
    if deployment and deployment.status in DeploymentStatus.ACTIVE:
        return error_response(
            "DEPLOYMENT_ACTIVE", "该项目已部署，请先停止部署后再删除", 409
        )

    active_run = AgentRun.query.filter(
        AgentRun.resource_type == "code_project",
        AgentRun.resource_id == project_id,
        AgentRun.status.in_(["running", "queued"]),
    ).first()
    if active_run:
        return error_response(
            "RUN_ACTIVE", "会话正在运行中，请等待结束或取消后再删除", 409
        )

    # Load and delete related runs individually so SQLAlchemy cascades
    # (steps/events/artifacts) are honored. Bulk .delete() bypasses ORM-level
    # cascade and fails on foreign-key constraints.
    ledger = CodeProjectLedger.query.filter_by(project_id=project_id).first()
    if ledger:
        db.session.delete(ledger)
    if deployment:
        db.session.delete(deployment)
    for run in AgentRun.query.filter_by(
        resource_type="code_project", resource_id=project_id
    ).all():
        db.session.delete(run)
    db.session.delete(project)
    db.session.commit()
    return success_response(None, "项目已删除")


@code_project_bp.route("/projects/<project_id>", methods=["PATCH"])
@jwt_required()
def update_project(project_id: str):
    """Update editable project-level fields."""
    project = _get_owned_project(project_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在", 404)

    data = request.get_json() or {}
    changed = [
        field
        for field in (
            "title",
            "requirement_input",
            "requirements_doc",
            "development_flow",
            "style_prompt",
            "ui_baseline_prompt",
            "confirmed_preview_url",
        )
        if field in data
    ]
    for field in changed:
        setattr(project, field, data.get(field))
    db.session.commit()
    # Record manual edits as new versions for the affected stages (deduped).
    record_versions_for_fields(project, changed)
    return success_response({"project": project.to_dict()}, "项目已保存")


@code_project_bp.route("/projects/<project_id>/preview-visibility", methods=["POST"])
@jwt_required()
def set_preview_visibility(project_id: str):
    """Toggle whether the project's built frontend preview is publicly reachable.

    ``public`` => ``/preview/<project_id>/`` serves the latest built site WITHOUT
    auth (the unguessable UUID is the share capability; revocable by flipping
    back). ``private`` => owner-token only. Only the rendered static site is ever
    exposed — source/zip stay on the JWT-protected ``/api/agent`` routes.
    """
    project = _get_owned_project(project_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在", 404)
    data = request.get_json() or {}
    project.visibility = "public" if bool(data.get("public")) else "private"
    db.session.commit()
    return success_response(
        {
            "visibility": project.visibility,
            "public": project.visibility == "public",
            "preview_path": f"/preview/{project.id}/",
        },
        "已更新预览可见性",
    )


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
    safe_record_stage_version(project, CodeStage.FLOW)
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
    safe_record_stage_version(project, CodeStage.DOCUMENTS)
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
    safe_record_stage_version(
        project, CodeStage.DOCUMENTS, source=CodeStageVersionSource.MANUAL_EDIT
    )
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
    safe_record_stage_version(project, CodeStage.STYLE)
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
        # Image upstream unavailable (e.g. Panlaxy "no available compatible
        # accounts"). Don't dead-end the flow: skip the thumbnails, adopt the
        # style prompt as the UI baseline, and advance to ui_confirmed so the
        # frontend development flow is unblocked without a manual confirm.
        project.set_preview_images([])
        project.ui_baseline_prompt = prompt
        project.status = CodeProjectStatus.UI_CONFIRMED
        db.session.commit()
        safe_record_stage_version(project, CodeStage.PREVIEW)
        return success_response(
            {
                "project": project.to_dict(),
                "preview_skipped": True,
                "preview_error": str(error),
            },
            "缩略图服务暂不可用，已用风格提示词作为 UI 基调，可直接进入前端开发",
        )
    project.set_preview_images(images)
    project.status = CodeProjectStatus.PREVIEW_READY
    db.session.commit()
    safe_record_stage_version(project, CodeStage.PREVIEW)
    return success_response(
        {"project": project.to_dict(), "preview_skipped": False}, "应用缩略图已生成"
    )


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
    safe_record_stage_version(
        project, CodeStage.PREVIEW, source=CodeStageVersionSource.MANUAL_EDIT
    )
    return success_response({"project": project.to_dict()}, "UI 基调已确认")


# --- inline section (partial) revision ----------------------------------------
# Rewrite only a user-selected span of an already-confirmed stage product. The
# model is given the whole document + the project's latest full-generation context
# ledger as context (so the tweak stays on-口径) but returns ONLY the replacement
# for the span; we splice it back at the resolved offsets, snapshot a new version,
# and return the exact changed range so the client can highlight just what moved.

# Text-primary stages that support section revision -> (CodeProject field, stage).
_SECTION_REVISION_FIELDS = {
    "requirements": ("requirements_doc", CodeStage.REQUIREMENTS),
    "flow": ("development_flow", CodeStage.FLOW),
    "style": ("style_prompt", CodeStage.STYLE),
}


def _read_section_revision_payload():
    """Parse + validate the shared section-revision body.

    Returns ``(selected_text, instruction, sel_start, sel_end, None)`` on success
    or ``(None, None, None, None, error_response)`` on a validation failure. The
    raw selection offsets are passed through untouched (coerced in ``_resolve_span``).
    """
    data = request.get_json() or {}
    selected_text = data.get("selected_text") or ""
    instruction = (data.get("instruction") or "").strip()
    if not selected_text.strip():
        return None, None, None, None, error_response(
            "VALIDATION_ERROR", "请先选中要修改的文字", 400
        )
    if not instruction:
        return None, None, None, None, error_response(
            "VALIDATION_ERROR", "请填写调整意见", 400
        )
    return selected_text, instruction, data.get("selection_start"), data.get("selection_end"), None


def _resolve_span(current_doc: str, start, end, selected_text: str):
    """Locate the span to replace, returning ``(start, end)`` or ``None``.

    Prefers the client-supplied offsets when they still bracket exactly the
    selected text (the document may have been re-generated/edited since), else
    falls back to the first verbatim occurrence. ``None`` means the selection no
    longer exists in the document — the caller asks the user to reselect.
    """
    length = len(current_doc)
    try:
        s, e = int(start), int(end)
    except (TypeError, ValueError):
        s = e = -1
    if 0 <= s <= e <= length and current_doc[s:e] == selected_text:
        return s, e
    idx = current_doc.find(selected_text)
    if idx >= 0:
        return idx, idx + len(selected_text)
    return None


@code_project_bp.route("/projects/<project_id>/stages/<stage>/revise-section", methods=["POST"])
@jwt_required()
def revise_stage_section(project_id: str, stage: str):
    """Apply an AI partial revision to a selected span of a text stage product."""
    if stage not in _SECTION_REVISION_FIELDS:
        return error_response("VALIDATION_ERROR", "该环节不支持局部修订", 400)
    project = _get_owned_project(project_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在", 404)

    selected_text, instruction, sel_start, sel_end, error = _read_section_revision_payload()
    if error:
        return error

    field, stage_key = _SECTION_REVISION_FIELDS[stage]
    current_doc = getattr(project, field) or ""
    if not current_doc.strip():
        return error_response("VALIDATION_ERROR", "当前环节暂无可修订的内容", 400)

    span = _resolve_span(current_doc, sel_start, sel_end, selected_text)
    if span is None:
        return error_response("VALIDATION_ERROR", "文档内容已变化，请重新选择要修改的文字", 400)

    user_id = get_jwt_identity()
    if not charge(
        user_id,
        pricing.CODE_SECTION_REVISION,
        "code_section_revise",
        "code_project",
        project.id,
        team_id=project.team_id,
    ):
        return error_response("INSUFFICIENT_CREDITS", "积分不足，无法进行局部修订", 402)

    try:
        service = get_code_generation_service()
        replacement = service.revise_section(
            stage,
            current_doc,
            selected_text,
            instruction,
            context_ledger=_load_ledger_for_project(project),
        )
        start, end = span
        new_doc = current_doc[:start] + replacement + current_doc[end:]
        setattr(project, field, new_doc)
        db.session.commit()
    except Exception:
        db.session.rollback()
        refund_credits(
            user_id,
            pricing.CODE_SECTION_REVISION,
            "code_section_revise",
            "code_project",
            project.id,
            team_id=project.team_id,
        )
        raise

    changed = new_doc != current_doc
    version = (
        safe_record_stage_version(
            project,
            stage_key,
            source=CodeStageVersionSource.PARTIAL_REVISION,
            note=f"局部修订：{instruction[:200]}",
        )
        if changed
        else None
    )
    return success_response(
        {
            "project": project.to_dict(),
            "version": version.to_dict() if version else None,
            "change": {"start": start, "end": start + len(replacement)} if changed else None,
        },
        "已应用局部修订" if changed else "内容未发生变化",
    )


@code_project_bp.route(
    "/projects/<project_id>/documents/<document_id>/revise-section", methods=["POST"]
)
@jwt_required()
def revise_document_section(project_id: str, document_id: str):
    """Apply an AI partial revision to a selected span of a single development document."""
    project = _get_owned_project(project_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在", 404)
    document = CodeDocument.query.filter_by(id=document_id, project_id=project.id).first()
    if not document:
        return error_response("NOT_FOUND", "文档不存在", 404)

    selected_text, instruction, sel_start, sel_end, error = _read_section_revision_payload()
    if error:
        return error

    current_doc = document.content or ""
    if not current_doc.strip():
        return error_response("VALIDATION_ERROR", "该文档暂无可修订的内容", 400)

    span = _resolve_span(current_doc, sel_start, sel_end, selected_text)
    if span is None:
        return error_response("VALIDATION_ERROR", "文档内容已变化，请重新选择要修改的文字", 400)

    user_id = get_jwt_identity()
    if not charge(
        user_id,
        pricing.CODE_SECTION_REVISION,
        "code_section_revise",
        "code_project",
        project.id,
        team_id=project.team_id,
    ):
        return error_response("INSUFFICIENT_CREDITS", "积分不足，无法进行局部修订", 402)

    try:
        service = get_code_generation_service()
        replacement = service.revise_section(
            "document",
            current_doc,
            selected_text,
            instruction,
            context_ledger=_load_ledger_for_project(project),
        )
        start, end = span
        new_doc = current_doc[:start] + replacement + current_doc[end:]
        document.content = new_doc
        db.session.commit()
    except Exception:
        db.session.rollback()
        refund_credits(
            user_id,
            pricing.CODE_SECTION_REVISION,
            "code_section_revise",
            "code_project",
            project.id,
            team_id=project.team_id,
        )
        raise

    changed = new_doc != current_doc
    # The documents stage versions the whole set, mirroring update_document.
    version = (
        safe_record_stage_version(
            project,
            CodeStage.DOCUMENTS,
            source=CodeStageVersionSource.PARTIAL_REVISION,
            note=f"局部修订：{document.title[:80]} · {instruction[:160]}",
        )
        if changed
        else None
    )
    return success_response(
        {
            "document": document.to_dict(),
            "version": version.to_dict() if version else None,
            "change": {"start": start, "end": start + len(replacement)} if changed else None,
        },
        "已应用局部修订" if changed else "内容未发生变化",
    )


# --- stage version history ----------------------------------------------------


@code_project_bp.route("/projects/<project_id>/versions", methods=["GET"])
@jwt_required()
def list_all_stage_versions(project_id: str):
    """Return every stage's version trail for the project (metadata only)."""
    project = _get_owned_project(project_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在", 404)
    versions = {
        stage: [version.to_dict() for version in list_stage_versions(project, stage)]
        for stage in CodeStage.ALL
    }
    return success_response({"versions": versions})


@code_project_bp.route("/projects/<project_id>/stages/<stage>/versions", methods=["GET"])
@jwt_required()
def list_one_stage_versions(project_id: str, stage: str):
    """List one stage's version trail (newest first, metadata only)."""
    if stage not in CodeStage.ALL:
        return error_response("VALIDATION_ERROR", "未知的环节", 400)
    project = _get_owned_project(project_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在", 404)
    versions = list_stage_versions(project, stage)
    return success_response({"versions": [version.to_dict() for version in versions]})


@code_project_bp.route(
    "/projects/<project_id>/stages/<stage>/versions/<version_id>", methods=["GET"]
)
@jwt_required()
def get_one_stage_version(project_id: str, stage: str, version_id: str):
    """Return a single version including its full content (for preview/diff)."""
    if stage not in CodeStage.ALL:
        return error_response("VALIDATION_ERROR", "未知的环节", 400)
    project = _get_owned_project(project_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在", 404)
    version = get_stage_version(project, stage, version_id)
    if not version:
        return error_response("NOT_FOUND", "版本不存在", 404)
    return success_response({"version": version.to_dict(include_content=True)})


@code_project_bp.route(
    "/projects/<project_id>/stages/<stage>/versions/<version_id>/activate", methods=["POST"]
)
@jwt_required()
def activate_one_stage_version(project_id: str, stage: str, version_id: str):
    """Roll a stage back to a historical version and make it current."""
    if stage not in CodeStage.ALL:
        return error_response("VALIDATION_ERROR", "未知的环节", 400)
    project = _get_owned_project(project_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在", 404)
    version = get_stage_version(project, stage, version_id)
    if not version:
        return error_response("NOT_FOUND", "版本不存在", 404)
    activate_stage_version(project, stage, version)
    return success_response(
        {"project": project.to_dict(), "version": version.to_dict()}, "已恢复到该版本"
    )


# ---------------------------------------------------------------------------
# Typed node-contract catalog (drives the canvas's typed-node palette)
# ---------------------------------------------------------------------------


@code_project_bp.route("/node-contracts", methods=["GET"])
@jwt_required()
def list_node_contracts():
    """Return the typed node-contract catalog (the composable 'components')."""
    from backend.services.agent.contracts.defaults import iter_default_node_contracts

    catalog = [c.to_catalog() for c in iter_default_node_contracts()]
    return success_response({"node_contracts": catalog})


@code_project_bp.route(
    "/projects/<project_id>/canvases/<canvas_id>/freeze", methods=["POST"]
)
@jwt_required()
def freeze_canvas(project_id: str, canvas_id: str):
    """Freeze the canvas's typed stage prompts to exact versions (reproducible runs)."""
    canvas = _get_owned_canvas(project_id, canvas_id)
    if not canvas:
        return error_response("NOT_FOUND", "画布不存在", 404)

    from backend.services.agent.contracts import freeze_stage_prompts
    from backend.services.agent.contracts.defaults import get_default_contract
    from backend.services.prompts import prompt_store

    new_nodes, pinned = freeze_stage_prompts(
        canvas.get_nodes(), get_default_contract, prompt_store.head_pin
    )
    canvas.set_nodes(new_nodes)
    db.session.commit()
    return success_response(
        {"canvas": canvas.to_dict(), "pinned": pinned},
        f"已固定 {pinned} 个阶段节点的提示词版本",
    )


# ---------------------------------------------------------------------------
# Remix canvas (n8n-style node graph) CRUD
# ---------------------------------------------------------------------------


@code_project_bp.route("/projects/<project_id>/canvases", methods=["GET"])
@jwt_required()
def list_canvases(project_id: str):
    """List the project's remix canvases (metadata only)."""
    project = _get_owned_project(project_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在", 404)
    canvases = project.canvases.order_by(CodeCanvas.created_at.desc()).all()
    return success_response(
        {"canvases": [canvas.to_dict(include_graph=False) for canvas in canvases]}
    )


@code_project_bp.route("/projects/<project_id>/canvases", methods=["POST"])
@jwt_required()
def create_canvas(project_id: str):
    """Create a remix canvas, optionally seeded with an initial graph."""
    project = _get_owned_project(project_id)
    if not project:
        return error_response("NOT_FOUND", "项目不存在", 404)
    data = request.get_json() or {}
    canvas = CodeCanvas(
        project_id=project.id,
        user_id=project.user_id,
        team_id=project.team_id,
        name=(data.get("name") or "未命名画布").strip()[:200],
    )
    canvas.set_nodes(data.get("nodes") or [])
    canvas.set_edges(data.get("edges") or [])
    canvas.set_viewport(data.get("viewport"))
    db.session.add(canvas)
    db.session.commit()
    return success_response({"canvas": canvas.to_dict()}, "画布已创建", 201)


@code_project_bp.route("/projects/<project_id>/canvases/<canvas_id>", methods=["GET"])
@jwt_required()
def get_canvas(project_id: str, canvas_id: str):
    """Return a canvas's full node/edge graph."""
    canvas = _get_owned_canvas(project_id, canvas_id)
    if not canvas:
        return error_response("NOT_FOUND", "画布不存在", 404)
    return success_response({"canvas": canvas.to_dict()})


@code_project_bp.route("/projects/<project_id>/canvases/<canvas_id>", methods=["PUT"])
@jwt_required()
def update_canvas(project_id: str, canvas_id: str):
    """Replace a canvas's graph (frontend debounce-saves the whole graph)."""
    canvas = _get_owned_canvas(project_id, canvas_id)
    if not canvas:
        return error_response("NOT_FOUND", "画布不存在", 404)
    data = request.get_json() or {}
    if "name" in data:
        canvas.name = (data.get("name") or "未命名画布").strip()[:200]
    if "nodes" in data:
        canvas.set_nodes(data.get("nodes") or [])
    if "edges" in data:
        canvas.set_edges(data.get("edges") or [])
    if "viewport" in data:
        canvas.set_viewport(data.get("viewport"))
    db.session.commit()
    return success_response({"canvas": canvas.to_dict()}, "画布已保存")


@code_project_bp.route("/projects/<project_id>/canvases/<canvas_id>", methods=["DELETE"])
@jwt_required()
def delete_canvas(project_id: str, canvas_id: str):
    """Delete a canvas."""
    canvas = _get_owned_canvas(project_id, canvas_id)
    if not canvas:
        return error_response("NOT_FOUND", "画布不存在", 404)
    db.session.delete(canvas)
    db.session.commit()
    return success_response({"deleted": canvas_id}, "画布已删除")


def _get_owned_canvas(project_id: str, canvas_id: str) -> CodeCanvas | None:
    project = _get_owned_project(project_id)
    if not project:
        return None
    return CodeCanvas.query.filter_by(id=canvas_id, project_id=project.id).first()


def _get_owned_project(project_id: str) -> CodeProject | None:
    user_id = get_jwt_identity()
    return CodeProject.query.filter_by(id=project_id, user_id=user_id).first()


def _load_ledger_for_project(project: CodeProject) -> str:
    """Render the project's latest full-generation context ledger for prompt injection.

    Read-only: a standalone revision route has no AgentRun of its own, so it
    recovers the established consensus from the most recent ``code_full_generation``
    run on this resource (mirroring how the frontend-project workflow reloads it).
    Best-effort — any lookup/parse hiccup yields an empty block (injection no-op).
    """
    try:
        prior = (
            AgentRun.query.filter_by(resource_id=project.id, workflow="code_full_generation")
            .order_by(AgentRun.created_at.desc())
            .first()
        )
        ledger = ContextLedger.load(prior.get_context_ledger() if prior else None)
        return "" if ledger.is_empty() else ledger.render_for_prompt()
    except Exception:  # noqa: BLE001 — ledger injection is auxiliary, never fatal
        return ""
