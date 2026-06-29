"""
App Space (应用空间) + secondary-development (二次开发) routes (mounted at /api/code).

The App Space is the management/iteration entry for ALREADY-DEPLOYED products —
NOT a new creation flow. Its data source is ``CodeProject`` + ``CodeDeployment``
(no new "app" master table, per the engineering standard). Every endpoint is
owner-only (``user_id == get_jwt_identity()``) so apps never cross users/teams.

    GET  /apps                                   list the user's deployed apps (filter/paginate)
    GET  /apps/<pid>                             one app's full context
    POST /apps/<pid>/iterations                  start a 二次开发 (impact analysis run)
    GET  /apps/<pid>/iterations                  list a project's iterations
    GET  /apps/<pid>/iterations/<iid>            one iteration (+ run states)
    POST /apps/<pid>/iterations/<iid>/confirm    confirm the plan → start the lane runs

The iteration lifecycle (analyze → confirm → generate → deploy → release) reuses
the existing AgentRun pipeline: the analysis is a ``code_app_iteration_analysis``
run; confirming starts the existing frontend/backend/middleware lane runs (reusing
the frozen contract, or re-synthesizing it when the change touches the API); the
deploy reuses ``POST /projects/<pid>/deploy`` (linked back via ``iteration_id``).
Run-driven state transitions are reconciled lazily on read so no callback is
needed and a crashed run can't strand an iteration in a non-terminal state.
"""
import logging
import zipfile

from flask import Blueprint, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from backend.extensions import db
from backend.models.agent import AgentArtifact, AgentRun, AgentRunStatus
from backend.models.code import CodeProject
from backend.models.code.fullstack import (
    CodeAppIteration,
    CodeDeployment,
    CodeProjectLedger,
    DeploymentStatus,
    ImpactScope,
    IterationChangeType,
    IterationStatus,
)
from backend.models.code.github import GitHubRepoLink
from backend.models.team import TeamMember
from backend.models.user import User
from backend.routes.code.fullstack_routes import (
    _PIPELINE,
    _active_pipeline_runs,
    _start_run,
    _trio_creation_lock,
)
from backend.services.agent.files import artifact_abs_path
from backend.services.code import deploy_service, middleware_service
from backend.services.code.fullstack import contract_service
from backend.services.lifecycle import drain_guard
from backend.utils.auth import is_admin
from backend.utils.response import error_response, success_response

# Latest published source zip per lane (prefer deploy-repaired backend source).
_LANE_SOURCE_REFS = {
    "frontend": ["code_frontend_project_zip"],
    "backend": ["code_backend_project_repaired_zip", "code_backend_project_zip"],
}
# Cap a single in-app code file view (larger files are downloaded, not inlined).
_MAX_CODE_FILE_BYTES = 512 * 1024

logger = logging.getLogger(__name__)

apps_bp = Blueprint("code_apps", __name__)


# --- helpers -----------------------------------------------------------------
def _owned_project(project_id: str, user_id: str) -> CodeProject | None:
    """Owner-only lookup — used to gate WRITE ops (iterate / confirm / redeploy)."""
    return CodeProject.query.filter_by(id=project_id, user_id=user_id).first()


def _is_team_member(team_id: str | None, user_id: str) -> bool:
    if not team_id:
        return False
    return (
        TeamMember.query.filter_by(team_id=team_id, user_id=user_id).first() is not None
    )


def _accessible_project(project_id: str, user_id: str) -> CodeProject | None:
    """Read-access lookup: the owner, a member of the project's team, OR an admin.

    Backs the App Space team-sharing requirement — a teammate can SEE (read) an app
    that belongs to a team they're in — plus read-only admin oversight of any app.
    Mutating endpoints keep using ``_owned_project`` so write access never crosses
    users.
    """
    project = db.session.get(CodeProject, project_id)
    if not project:
        return None
    if (
        project.user_id == user_id
        or _is_team_member(project.team_id, user_id)
        or is_admin(user_id)
    ):
        return project
    return None


def _run_state(run_id: str | None) -> dict | None:
    """Compact {id, workflow, status} for a run id (owner scoping done upstream)."""
    if not run_id:
        return None
    run = db.session.get(AgentRun, run_id)
    if not run:
        return None
    return {"id": run.id, "workflow": run.workflow, "status": run.status}


def _reconcile_iteration(iteration: CodeAppIteration) -> CodeAppIteration:
    """Advance a non-terminal iteration based on the state of its runs/deployment.

    Lazy reconciliation (on read) keeps the state machine honest without a
    completion callback: a failed analysis/lane/deploy run promotes the iteration
    to ``failed``; a completed deploy with a RUNNING deployment promotes it to
    ``released``.
    """
    status = iteration.status
    changed = False

    if status == IterationStatus.ANALYZING:
        run = db.session.get(AgentRun, iteration.analysis_run_id) if iteration.analysis_run_id else None
        if run and run.status in (AgentRunStatus.FAILED, AgentRunStatus.CANCELLED):
            iteration.status = IterationStatus.FAILED
            iteration.error_message = iteration.error_message or "影响分析失败"
            changed = True
        # A COMPLETED analysis run already set AWAITING_PLAN_APPROVAL itself.

    elif status == IterationStatus.GENERATING:
        lane_ids = iteration.lane_run_ids()
        if lane_ids:
            runs = AgentRun.query.filter(AgentRun.id.in_(list(lane_ids.values()))).all()
            states = [r.status for r in runs]
            if states and all(s in AgentRunStatus.TERMINAL for s in states):
                if any(s in (AgentRunStatus.FAILED, AgentRunStatus.CANCELLED) for s in states):
                    iteration.status = IterationStatus.FAILED
                    iteration.error_message = iteration.error_message or "生成阶段失败"
                    changed = True
                # else: all lanes ok — stay GENERATING (the user/UI triggers deploy).

    elif status == IterationStatus.STAGING_DEPLOYING:
        run = db.session.get(AgentRun, iteration.deploy_run_id) if iteration.deploy_run_id else None
        if run and run.status in AgentRunStatus.TERMINAL:
            deployment = CodeDeployment.query.filter_by(project_id=iteration.project_id).first()
            if run.status == AgentRunStatus.COMPLETED and deployment and deployment.status == DeploymentStatus.RUNNING:
                iteration.status = IterationStatus.RELEASED
            else:
                iteration.status = IterationStatus.FAILED
                iteration.error_message = iteration.error_message or "部署未成功"
            changed = True

    if changed:
        db.session.commit()
    return iteration


def _generation_ready(iteration: CodeAppIteration) -> bool:
    """True when all requested lane runs finished OK and a deploy can start."""
    if iteration.status != IterationStatus.GENERATING:
        return False
    lane_ids = iteration.lane_run_ids()
    if not lane_ids:
        return False
    runs = AgentRun.query.filter(AgentRun.id.in_(list(lane_ids.values()))).all()
    states = [r.status for r in runs]
    return bool(states) and all(
        s in (AgentRunStatus.COMPLETED, AgentRunStatus.PARTIAL) for s in states
    )


def _iteration_view(iteration: CodeAppIteration) -> dict:
    """Iteration dict augmented with compact run states + a deploy-ready flag."""
    data = iteration.to_dict()
    data["runs"] = {
        "analysis": _run_state(iteration.analysis_run_id),
        "frontend": _run_state(iteration.frontend_run_id),
        "backend": _run_state(iteration.backend_run_id),
        "middleware": _run_state(iteration.middleware_run_id),
        "deploy": _run_state(iteration.deploy_run_id),
    }
    data["generation_ready"] = _generation_ready(iteration)
    return data


def _app_list_item(
    deployment: CodeDeployment, project: CodeProject, owner: User | None = None
) -> dict:
    return {
        "project_id": project.id,
        "title": project.title,
        "visibility": project.visibility,
        "team_id": project.team_id,
        # Who created the app — surfaced so a team view can label cross-member apps.
        "owner": (
            {
                "id": owner.id,
                "display_name": owner.display_name,
                "avatar_url": owner.avatar_url,
            }
            if owner
            else None
        ),
        "deployment_status": deployment.status,
        "health": deployment.health or "unknown",
        "is_running": deployment.status == DeploymentStatus.RUNNING,
        "api_base_path": deployment.api_base_path or f"/app/{project.id}/api",
        "preview_url": f"/preview/{project.id}/",
        "deployed_at": deployment.deployed_at.isoformat() + "Z" if deployment.deployed_at else None,
        "updated_at": project.updated_at.isoformat() + "Z" if project.updated_at else None,
    }


# --- App Space list / detail -------------------------------------------------
@apps_bp.route("/apps", methods=["GET"])
@jwt_required()
def list_apps():
    """List deployed apps in the requested scope (CodeDeployment ⨝ CodeProject).

    Scope is governed by ``?scope=`` / ``?team_id=``:
      - ``scope=all`` (admins only) → PLATFORM: every deployed app across all
        users/teams (read-only oversight). Silently ignored for non-admins.
      - ``team_id`` absent → PERSONAL: the caller's own apps that aren't shared to
        any team (``CodeDeployment.user_id == me`` AND ``CodeProject.team_id IS NULL``).
      - ``team_id`` present → TEAM: every app belonging to that team (``CodeProject.team_id ==
        team_id``), created by ANY member — gated on the caller being a member.

    Also supports ``?status=``, ``?health=``, ``?q=`` (title search) and
    ``?limit/&offset`` pagination.
    """
    user_id = get_jwt_identity()
    team_id = (request.args.get("team_id") or "").strip() or None
    admin_all = (request.args.get("scope") or "").strip().lower() == "all" and is_admin(user_id)
    if team_id and not _is_team_member(team_id, user_id):
        return error_response("FORBIDDEN", "无权访问该团队的应用空间", 403)

    query = (
        db.session.query(CodeDeployment, CodeProject, User)
        .join(CodeProject, CodeProject.id == CodeDeployment.project_id)
        .outerjoin(User, User.id == CodeProject.user_id)
    )
    if admin_all:
        pass  # no owner/team filter — list every deployed app
    elif team_id:
        query = query.filter(CodeProject.team_id == team_id)
    else:
        query = query.filter(
            CodeDeployment.user_id == user_id,
            CodeProject.team_id.is_(None),
        )

    status = request.args.get("status")
    if status:
        query = query.filter(CodeDeployment.status == status)
    health = request.args.get("health")
    if health:
        query = query.filter(CodeDeployment.health == health)
    search = (request.args.get("q") or "").strip()
    if search:
        query = query.filter(CodeProject.title.ilike(f"%{search}%"))

    try:
        limit = min(max(int(request.args.get("limit", 20)), 1), 100)
    except (TypeError, ValueError):
        limit = 20
    try:
        offset = max(int(request.args.get("offset", 0)), 0)
    except (TypeError, ValueError):
        offset = 0

    total = query.count()
    rows = (
        query.order_by(CodeDeployment.updated_at.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )
    apps = [_app_list_item(dep, proj, owner) for dep, proj, owner in rows]
    return success_response(
        {"apps": apps, "total": total, "limit": limit, "offset": offset}
    )


@apps_bp.route("/apps/<project_id>", methods=["GET"])
@jwt_required()
def get_app(project_id: str):
    """One deployed app's full context (status, runs, contract tech stack, github)."""
    user_id = get_jwt_identity()
    project = _accessible_project(project_id, user_id)
    if not project:
        return error_response("NOT_FOUND", "应用不存在或无权访问", 404)

    deployment = CodeDeployment.query.filter_by(project_id=project_id).first()
    ledger = CodeProjectLedger.query.filter_by(project_id=project_id).first()
    tech_stack = (ledger.get_api_contract().get("tech_stack") if ledger else {}) or {}

    def latest(workflow: str) -> dict | None:
        # Scope by the project's owner (not the viewer) so a teammate sees the app's
        # real run history rather than an empty list.
        run = (
            AgentRun.query.filter_by(
                resource_id=project_id, user_id=project.user_id, workflow=workflow
            )
            .order_by(AgentRun.created_at.desc())
            .first()
        )
        return run.to_dict() if run else None

    github_link = GitHubRepoLink.query.filter_by(project_id=project_id).first()
    github = None
    if github_link:
        github = github_link.to_dict()
        try:
            from backend.services.code.github.sync_service import dev_branch_name

            github["dev_branch"] = dev_branch_name()
        except Exception:  # noqa: BLE001
            github["dev_branch"] = None

    iterations = (
        CodeAppIteration.query.filter_by(project_id=project_id)
        .order_by(CodeAppIteration.created_at.desc())
        .limit(20)
        .all()
    )
    iteration_views = [_iteration_view(_reconcile_iteration(it)) for it in iterations]

    summary = (project.requirement_input or project.requirements_doc or "")[:400]
    return success_response(
        {
            "project": {
                "id": project.id,
                "title": project.title,
                "requirement_summary": summary,
                "status": project.status,
                "visibility": project.visibility,
                "created_at": project.created_at.isoformat() + "Z" if project.created_at else None,
                "updated_at": project.updated_at.isoformat() + "Z" if project.updated_at else None,
            },
            "deployment": deployment.to_dict() if deployment else None,
            "preview_url": f"/preview/{project_id}/",
            "api_base_path": (deployment.api_base_path if deployment else None) or f"/app/{project_id}/api",
            "tech_stack": tech_stack,
            "runs": {
                "frontend": latest("code_frontend_project_generation"),
                "backend": latest("code_backend_project_generation"),
                "middleware": latest("code_middleware_provisioning"),
                "deploy": latest("code_fullstack_deploy"),
            },
            "github": github,
            "iterations": iteration_views,
        }
    )


# --- Secondary development (iterations) --------------------------------------
def _owned_iteration(project_id: str, iteration_id: str, user_id: str) -> CodeAppIteration | None:
    return CodeAppIteration.query.filter_by(
        id=iteration_id, project_id=project_id, user_id=user_id
    ).first()


@apps_bp.route("/apps/<project_id>/iterations", methods=["POST"])
@jwt_required()
def create_iteration(project_id: str):
    """Start a 二次开发: create the iteration record and kick off the analysis run."""
    drained = drain_guard()  # starts a background analysis run — refuse while draining
    if drained:
        return drained
    user_id = get_jwt_identity()
    project = _owned_project(project_id, user_id)
    if not project:
        return error_response("NOT_FOUND", "应用不存在或无权访问", 404)

    deployment = CodeDeployment.query.filter_by(project_id=project_id).first()
    if not deployment:
        return error_response(
            "VALIDATION_ERROR", "二次开发需从已部署的应用发起，请先完成部署", 400
        )

    body = request.get_json(silent=True) or {}
    instruction = (body.get("instruction") or "").strip()
    if not instruction:
        return error_response("VALIDATION_ERROR", "请填写变更说明", 400)
    change_type = (body.get("change_type") or IterationChangeType.OTHER).strip()
    if change_type not in IterationChangeType.ALL:
        return error_response("VALIDATION_ERROR", f"未知的变更类型：{change_type}", 400)
    scope_override = body.get("impact_scope")
    if scope_override is not None and scope_override not in ImpactScope.ALL:
        return error_response("VALIDATION_ERROR", f"未知的影响范围：{scope_override}", 400)

    iteration = CodeAppIteration(
        project_id=project_id,
        user_id=user_id,
        team_id=project.team_id,
        base_deployment_id=deployment.id,
        instruction=instruction,
        change_type=change_type,
        impact_scope=scope_override,
        allow_contract_change=bool(body.get("allow_contract_change", False)),
        allow_db_change=bool(body.get("allow_db_change", False)),
        deploy_to_prod=bool(body.get("deploy_to_prod", True)),
        status=IterationStatus.DRAFT,
    )
    db.session.add(iteration)
    db.session.commit()

    run = _start_run(
        user_id,
        project.team_id,
        "code_app_iteration_analysis",
        project_id,
        {"iteration_id": iteration.id, "title": f"二次开发分析：{instruction[:40]}"},
    )
    if run is None:
        iteration.status = IterationStatus.FAILED
        iteration.error_message = "积分不足，无法启动影响分析"
        db.session.commit()
        return error_response("INSUFFICIENT_CREDITS", "积分不足，无法启动影响分析", 402)

    iteration.analysis_run_id = run.id
    iteration.status = IterationStatus.ANALYZING
    db.session.commit()

    return success_response(
        {
            "iteration": _iteration_view(iteration),
            "stream_url": f"/api/agent/runs/{run.id}/stream",
        },
        "二次开发影响分析已启动",
        201,
    )


@apps_bp.route("/apps/<project_id>/iterations", methods=["GET"])
@jwt_required()
def list_iterations(project_id: str):
    """List a project's iterations (owner or teammate), reconciling run-driven state."""
    user_id = get_jwt_identity()
    project = _accessible_project(project_id, user_id)
    if not project:
        return error_response("NOT_FOUND", "应用不存在或无权访问", 404)
    iterations = (
        CodeAppIteration.query.filter_by(project_id=project_id)
        .order_by(CodeAppIteration.created_at.desc())
        .limit(50)
        .all()
    )
    views = [_iteration_view(_reconcile_iteration(it)) for it in iterations]
    return success_response({"iterations": views})


@apps_bp.route("/apps/<project_id>/iterations/<iteration_id>", methods=["GET"])
@jwt_required()
def get_iteration(project_id: str, iteration_id: str):
    """One iteration with its analysis, plan and compact run states (owner or teammate)."""
    user_id = get_jwt_identity()
    if not _accessible_project(project_id, user_id):
        return error_response("NOT_FOUND", "应用不存在或无权访问", 404)
    iteration = CodeAppIteration.query.filter_by(
        id=iteration_id, project_id=project_id
    ).first()
    if not iteration:
        return error_response("NOT_FOUND", "迭代记录不存在或无权访问", 404)
    _reconcile_iteration(iteration)
    return success_response({"iteration": _iteration_view(iteration)})


@apps_bp.route("/apps/<project_id>/iterations/<iteration_id>/confirm", methods=["POST"])
@jwt_required()
def confirm_iteration(project_id: str, iteration_id: str):
    """Confirm the plan and start the requested generation lane runs.

    Reuses the full-stack pipeline: the lanes derive from the (possibly
    user-overridden) impact scope; the shared contract is re-synthesized when the
    change touches the API/DB AND the user allowed it, otherwise the frozen
    contract is reused so the regenerated lanes stay aligned with the rest.
    """
    drained = drain_guard()  # starts background lane runs — refuse while draining
    if drained:
        return drained
    user_id = get_jwt_identity()
    project = _owned_project(project_id, user_id)
    if not project:
        return error_response("NOT_FOUND", "应用不存在或无权访问", 404)
    iteration = _owned_iteration(project_id, iteration_id, user_id)
    if not iteration:
        return error_response("NOT_FOUND", "迭代记录不存在或无权访问", 404)

    _reconcile_iteration(iteration)
    if iteration.status != IterationStatus.AWAITING_PLAN_APPROVAL:
        return error_response(
            "VALIDATION_ERROR", f"当前状态（{iteration.status}）不可确认执行计划", 409
        )

    if not project.requirements_doc or not project.development_flow:
        return error_response(
            "VALIDATION_ERROR", "项目缺少需求文档/开发流程，无法重新生成", 400
        )

    body = request.get_json(silent=True) or {}
    scope = body.get("impact_scope") or iteration.impact_scope or ImpactScope.BACKEND
    if scope not in ImpactScope.ALL:
        return error_response("VALIDATION_ERROR", f"未知的影响范围：{scope}", 400)
    allow_contract = bool(body.get("allow_contract_change", iteration.allow_contract_change))
    allow_db = bool(body.get("allow_db_change", iteration.allow_db_change))

    analysis = iteration.get_analysis()
    lanes = ImpactScope.lanes_for(scope)

    # Re-synthesize the shared contract only when the change touches the API or DB
    # AND the user allowed the corresponding change — otherwise reuse the frozen
    # contract so the regenerated lanes stay aligned with the untouched ones.
    needs_contract = bool(analysis.get("contract_change") and allow_contract) or bool(
        analysis.get("database_change") and allow_db
    )
    try:
        ledger = contract_service.ensure_contract(
            project, user_id, project.team_id, force=needs_contract
        )
    except Exception as error:  # noqa: BLE001
        logger.error("contract synthesis failed for iteration: %s", error, exc_info=True)
        return error_response("SERVER_ERROR", f"共享 API 契约合成失败：{error}", 500)
    from backend.models.code.fullstack import ContractStatus

    if ledger.contract_status != ContractStatus.READY:
        return error_response("SERVER_ERROR", "共享 API 契约未就绪", 500)

    # Persist any overrides before starting the runs.
    iteration.impact_scope = scope
    iteration.allow_contract_change = allow_contract
    iteration.allow_db_change = allow_db

    runs: dict[str, str] = {}
    with _trio_creation_lock:
        active = _active_pipeline_runs(project_id, user_id)
        for key in lanes:
            workflow, _cost = _PIPELINE[key]
            existing = active.get(workflow)
            if existing:
                runs[key] = existing.id
                continue
            run = _start_run(
                user_id,
                project.team_id,
                workflow,
                project_id,
                {"pipeline": key, "iteration_id": iteration.id},
            )
            if run is None:
                return error_response("INSUFFICIENT_CREDITS", "积分不足，无法启动生成", 402)
            runs[key] = run.id

    iteration.frontend_run_id = runs.get("frontend") or iteration.frontend_run_id
    iteration.backend_run_id = runs.get("backend") or iteration.backend_run_id
    iteration.middleware_run_id = runs.get("middleware") or iteration.middleware_run_id
    iteration.status = IterationStatus.GENERATING
    iteration.error_message = None
    db.session.commit()

    return success_response(
        {
            "iteration": _iteration_view(iteration),
            "runs": runs,
            "stream_urls": {k: f"/api/agent/runs/{v}/stream" for k, v in runs.items()},
        },
        "执行计划已确认，开始按影响范围重新生成",
        201,
    )


# --- Resources / database / code entries -------------------------------------
def _latest_source_artifact(project_id: str, lane: str) -> AgentArtifact | None:
    """Latest published source-zip artifact for a lane (prefers repaired backend)."""
    for ref in _LANE_SOURCE_REFS.get(lane, []):
        art = (
            AgentArtifact.query.filter_by(domain_ref_type=ref, domain_ref_id=project_id)
            .order_by(AgentArtifact.created_at.desc())
            .first()
        )
        if art and art.storage_path:
            return art
    return None


def _zip_namelist(art: AgentArtifact) -> list[str]:
    try:
        with zipfile.ZipFile(artifact_abs_path(art.storage_path), "r") as archive:
            return [n for n in archive.namelist() if not n.endswith("/")]
    except Exception:  # noqa: BLE001
        return []


def _zip_read(art: AgentArtifact, path: str) -> bytes | None:
    try:
        with zipfile.ZipFile(artifact_abs_path(art.storage_path), "r") as archive:
            return archive.read(path)
    except Exception:  # noqa: BLE001 — missing member / corrupt zip
        return None


def _source_summary(art: AgentArtifact | None) -> dict | None:
    if not art:
        return None
    return {
        "artifact_id": art.id,
        "filename": art.filename,
        "file_count": len(_zip_namelist(art)),
        "download_url": f"/api/agent/artifacts/{art.id}/file?download=1",
        "created_at": art.created_at.isoformat() + "Z" if art.created_at else None,
    }


@apps_bp.route("/apps/<project_id>/resources", methods=["GET"])
@jwt_required()
def app_resources(project_id: str):
    """Frontend / backend / database resources backing a deployed app."""
    user_id = get_jwt_identity()
    project = _accessible_project(project_id, user_id)
    if not project:
        return error_response("NOT_FOUND", "应用不存在或无权访问", 404)

    deployment = CodeDeployment.query.filter_by(project_id=project_id).first()
    db_name = deployment.db_name if deployment else None
    db_url = middleware_service.project_database_url(db_name)
    table_count = middleware_service.count_tables(db_url) if db_url else None
    database = {
        "engine": "postgres" if db_name else "sqlite",
        "db_name": db_name,
        "redis_prefix": deployment.redis_prefix if deployment else None,
        "table_count": table_count,
        "introspectable": bool(db_url),
    }

    return success_response(
        {
            "frontend": _source_summary(_latest_source_artifact(project_id, "frontend")),
            "backend": _source_summary(_latest_source_artifact(project_id, "backend")),
            "database": database,
            "preview_url": f"/preview/{project_id}/",
            "api_base_path": (deployment.api_base_path if deployment else None) or f"/app/{project_id}/api",
        }
    )


@apps_bp.route("/apps/<project_id>/database", methods=["GET"])
@jwt_required()
def app_database(project_id: str):
    """Read-only schema introspection of the app's database (数据库管理入口)."""
    user_id = get_jwt_identity()
    project = _accessible_project(project_id, user_id)
    if not project:
        return error_response("NOT_FOUND", "应用不存在或无权访问", 404)
    deployment = CodeDeployment.query.filter_by(project_id=project_id).first()
    db_name = deployment.db_name if deployment else None
    db_url = middleware_service.project_database_url(db_name)
    if not db_url:
        return success_response(
            {"engine": "sqlite" if deployment else "unknown", "available": False, "tables": [], "db_name": db_name}
        )
    info = middleware_service.introspect_database(db_url)
    info["db_name"] = db_name
    info["redis_prefix"] = deployment.redis_prefix if deployment else None
    return success_response(info)


@apps_bp.route("/apps/<project_id>/database/tables/<table>/rows", methods=["GET"])
@jwt_required()
def app_database_rows(project_id: str, table: str):
    """Read-only sample rows from one table of the app's database."""
    user_id = get_jwt_identity()
    project = _accessible_project(project_id, user_id)
    if not project:
        return error_response("NOT_FOUND", "应用不存在或无权访问", 404)
    deployment = CodeDeployment.query.filter_by(project_id=project_id).first()
    db_url = middleware_service.project_database_url(deployment.db_name if deployment else None)
    data = middleware_service.sample_rows(db_url, table, request.args.get("limit", 20))
    return success_response(data)


@apps_bp.route("/apps/<project_id>/code", methods=["GET"])
@jwt_required()
def app_code(project_id: str):
    """List the files of the app's latest frontend/backend source (应用代码入口)."""
    user_id = get_jwt_identity()
    project = _accessible_project(project_id, user_id)
    if not project:
        return error_response("NOT_FOUND", "应用不存在或无权访问", 404)
    lane = request.args.get("lane", "frontend")
    if lane not in _LANE_SOURCE_REFS:
        return error_response("VALIDATION_ERROR", f"未知的代码泳道：{lane}", 400)
    art = _latest_source_artifact(project_id, lane)
    if not art:
        return success_response({"lane": lane, "artifact_id": None, "files": []})
    return success_response(
        {
            "lane": lane,
            "artifact_id": art.id,
            "download_url": f"/api/agent/artifacts/{art.id}/file?download=1",
            "files": sorted(_zip_namelist(art)),
        }
    )


@apps_bp.route("/apps/<project_id>/code/file", methods=["GET"])
@jwt_required()
def app_code_file(project_id: str):
    """Return one source file's text content (in-app code viewer)."""
    user_id = get_jwt_identity()
    project = _accessible_project(project_id, user_id)
    if not project:
        return error_response("NOT_FOUND", "应用不存在或无权访问", 404)
    lane = request.args.get("lane", "frontend")
    if lane not in _LANE_SOURCE_REFS:
        return error_response("VALIDATION_ERROR", f"未知的代码泳道：{lane}", 400)
    path = (request.args.get("path") or "").strip()
    if not path:
        return error_response("VALIDATION_ERROR", "缺少文件路径 path", 400)
    art = _latest_source_artifact(project_id, lane)
    if not art:
        return error_response("NOT_FOUND", "尚无该泳道的源码产物", 404)
    raw = _zip_read(art, path)
    if raw is None:
        return error_response("NOT_FOUND", "文件不存在", 404)
    truncated = len(raw) > _MAX_CODE_FILE_BYTES
    body = raw[:_MAX_CODE_FILE_BYTES]
    try:
        content = body.decode("utf-8")
        is_binary = False
    except UnicodeDecodeError:
        content = ""
        is_binary = True
    return success_response(
        {
            "lane": lane,
            "path": path,
            "size": len(raw),
            "content": content,
            "is_binary": is_binary,
            "truncated": truncated,
        }
    )


# --- Operations: stop / logs / health re-probe -------------------------------
@apps_bp.route("/apps/<project_id>/stop", methods=["POST"])
@jwt_required()
def stop_app(project_id: str):
    """Stop a deployed app's container (owner-only). Keeps the db for redeploy."""
    user_id = get_jwt_identity()
    project = _owned_project(project_id, user_id)
    if not project:
        return error_response("NOT_FOUND", "应用不存在或无权访问", 404)
    deploy_service.stop_deployment(project_id)
    return success_response({"status": DeploymentStatus.STOPPED}, "已停止部署")


@apps_bp.route("/apps/<project_id>/logs", methods=["GET"])
@jwt_required()
def app_logs(project_id: str):
    """Read-only tail of the deployed container's runtime logs (owner-only)."""
    user_id = get_jwt_identity()
    project = _owned_project(project_id, user_id)
    if not project:
        return error_response("NOT_FOUND", "应用不存在或无权访问", 404)
    data = deploy_service.container_logs(project_id, request.args.get("tail", 200))
    return success_response(data)


@apps_bp.route("/apps/<project_id>/health/refresh", methods=["POST"])
@jwt_required()
def app_health_refresh(project_id: str):
    """Re-probe the deployed app's health and persist it (owner-only)."""
    user_id = get_jwt_identity()
    project = _owned_project(project_id, user_id)
    if not project:
        return error_response("NOT_FOUND", "应用不存在或无权访问", 404)
    data = deploy_service.probe_health(project_id)
    return success_response(data)
