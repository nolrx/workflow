"""
Code domain workflow — ``code_fullstack_deploy``.

The JOIN point of the full-stack pipeline. After the three concurrent generation
runs (frontend / backend / middleware) complete, this run atomically brings the
generated app up behind a reverse proxy, in dependency order, with rollback on
any failure:

    provision middleware namespace → build the backend image → run the container
      → health check → register the /app/<pid>/api reverse proxy

On success the generated frontend (served at /preview/<pid>/) can call the live
backend for real. The heavy lifting lives in ``deploy_service``; this workflow
narrates progress onto the timeline and publishes the deploy metadata.

One counted step (``fs_deploy``) with internal phases reflected in the progress
bar (provision / build / start / done).
"""
import logging

from backend.extensions import db
from backend.models.agent import (
    AgentArtifactType,
    AgentEventLevel,
    AgentEventType,
    AgentRun,
    AgentRunStatus,
)
from backend.models.code import CodeProject
from backend.services.code import deploy_service

logger = logging.getLogger(__name__)

TOTAL_STEPS = 4  # provision, build, start, done — driven by deploy phases
_PHASE_PROGRESS = {"provision": 1, "build": 2, "start": 3, "health": 3, "done": 4}


def run_code_fullstack_deploy_workflow(ctx, recorder) -> dict:
    """Atomically deploy the generated full-stack app."""
    if not ctx.resource_id:
        raise ValueError("缺少 resource_id：部署需要一个已有的 Code 项目")
    project = CodeProject.query.filter_by(id=ctx.resource_id, user_id=ctx.user_id).first()
    if not project:
        raise ValueError("项目不存在或无权访问")
    project_id = project.id

    def set_progress(completed: int, current: str) -> None:
        run = db.session.get(AgentRun, ctx.run_id)
        if run:
            run.set_progress({
                "total_steps": TOTAL_STEPS, "completed_steps": completed,
                "failed_steps": 0, "current_step": current,
            })
            db.session.commit()

    with recorder.step(
        "fs_deploy", "部署 Agent", "publisher", 1,
        input_summary="原子化部署:中间件 → 后端容器 → 健康检查 → 反代接入",
    ) as step:
        def on_phase(phase: str, message: str, payload: dict) -> None:
            recovery = phase in ("rollback",)
            recorder.emit(
                AgentEventType.WARNING if recovery else AgentEventType.PROGRESS,
                level=AgentEventLevel.WARNING if recovery else AgentEventLevel.INFO,
                step_id=step.id, message=message, payload={"phase": phase, **(payload or {})},
            )
            completed = _PHASE_PROGRESS.get(phase, 0)
            if completed:
                set_progress(completed, phase)

        result = deploy_service.deploy(
            project, ctx.user_id, ctx.team_id,
            on_phase=on_phase, is_cancelled=ctx.is_cancelled,
        )

        if result.get("status") == deploy_service.DeploymentStatus.STOPPED:
            recorder.emit(AgentEventType.WARNING, level=AgentEventLevel.WARNING, step_id=step.id,
                          message="部署已取消并回滚")
            return {"status": AgentRunStatus.CANCELLED, "resource_id": project_id}

        if not result.get("success"):
            # deploy_service already rolled back; surface the reason and fail (refunded).
            raise RuntimeError(f"原子部署失败(已回滚):{result.get('error') or '未知错误'}")

        # Record which run performed the deploy.
        dep = deploy_service.get_deployment(project_id)
        if dep:
            dep.deploy_run_id = ctx.run_id
            db.session.commit()

        preview_url = result.get("preview_url") or f"/preview/{project_id}/"
        api_base = result.get("api_base")
        step.add_artifact(
            AgentArtifactType.JSON, "全栈部署元数据",
            content_json={
                "preview_url": preview_url,
                "api_base": api_base,
                "container": result.get("container"),
                "image": result.get("image"),
                "middleware": result.get("middleware"),
                "delivery": "fullstack-deploy",
                "status": result.get("status"),
            },
            filename="fullstack_deploy_meta.json",
            preview_url=preview_url,
            domain_ref_type="code_deploy_meta", domain_ref_id=project_id,
        )
        set_progress(TOTAL_STEPS, "done")
        step.set_output(
            output_summary=f"全栈应用已部署并通过健康检查:前端经 {api_base} 实时调用后端。",
            reasoning_summary="有序原子部署:建库 → docker build 后端镜像 → 启动长驻容器 → 健康检查 → 接入反代;任一步失败已回滚。",
            self_check=f"容器 {result.get('container')};api_base={api_base};预览={preview_url}",
            next_action="在预览中验证前后端联通。",
        )

    return {"status": AgentRunStatus.COMPLETED, "resource_id": project_id}
