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

import io
import logging
import zipfile

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

TOTAL_STEPS = 7  # provision, migrate, build, start, health, smoke, itest — driven by deploy phases
_PHASE_PROGRESS = {
    "provision": 1,
    "migrate": 2,
    "build": 3,
    "start": 4,
    "health": 5,
    "smoke": 6,
    "itest": 7,
    "done": 7,
}


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
            run.set_progress(
                {
                    "total_steps": TOTAL_STEPS,
                    "completed_steps": completed,
                    "failed_steps": 0,
                    "current_step": current,
                }
            )
            db.session.commit()

    with recorder.step(
        "fs_deploy",
        "部署 Agent",
        "publisher",
        1,
        input_summary="原子化部署:中间件 → 后端容器 → 健康检查 → 反代接入",
    ) as step:

        def on_phase(phase: str, message: str, payload: dict) -> None:
            recovery = phase in ("rollback",)
            recorder.emit(
                AgentEventType.WARNING if recovery else AgentEventType.PROGRESS,
                level=AgentEventLevel.WARNING if recovery else AgentEventLevel.INFO,
                step_id=step.id,
                message=message,
                payload={"phase": phase, **(payload or {})},
            )
            completed = _PHASE_PROGRESS.get(phase, 0)
            if completed:
                set_progress(completed, phase)

        result = deploy_service.deploy(
            project,
            ctx.user_id,
            ctx.team_id,
            on_phase=on_phase,
            is_cancelled=ctx.is_cancelled,
            run_id=ctx.run_id,
        )

        if result.get("status") == deploy_service.DeploymentStatus.STOPPED:
            recorder.emit(
                AgentEventType.WARNING,
                level=AgentEventLevel.WARNING,
                step_id=step.id,
                message="部署已取消并回滚",
            )
            return {"status": AgentRunStatus.CANCELLED, "resource_id": project_id}

        if not result.get("success"):
            # deploy_service already rolled back; surface the reason and fail (refunded).
            raise RuntimeError(f"应用部署失败(已回滚):{result.get('error') or '未知错误'}")

        # Record which run performed the deploy.
        dep = deploy_service.get_deployment(project_id)
        if dep:
            dep.deploy_run_id = ctx.run_id
            db.session.commit()

        preview_url = result.get("preview_url") or f"/preview/{project_id}/"
        api_base = result.get("api_base")
        step.add_artifact(
            AgentArtifactType.JSON,
            "全栈部署元数据",
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
            domain_ref_type="code_deploy_meta",
            domain_ref_id=project_id,
        )

        # PROMOTE: if the deploy auto-repaired the backend, publish the repaired
        # source (a NEW domain_ref_type — never reuse code_backend_project_zip) so
        # download / GitHub sync / re-deploy use the fixed code, plus a fix summary
        # for the timeline / panel. Logical overwrite, physical additive: the original
        # generation artifacts and their replay are untouched.
        if result.get("repaired") and result.get("repaired_source"):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                for rel, content in result["repaired_source"].items():
                    archive.writestr(rel, content or b"")
            step.add_artifact(
                AgentArtifactType.TEXT,
                "后端工程源码（部署自动修复版，zip）",
                filename="backend_project_repaired.zip",
                mime_type="application/zip",
                write_file=True,
                content_bytes=buffer.getvalue(),
                domain_ref_type="code_backend_project_repaired_zip",
                domain_ref_id=project_id,
            )
            step.add_artifact(
                AgentArtifactType.JSON,
                "部署自动修复摘要",
                content_json=result.get("fix_summary") or {},
                filename="deploy_fix_summary.json",
                domain_ref_type="code_deploy_fix_summary",
                domain_ref_id=project_id,
            )
        set_progress(TOTAL_STEPS, "done")
        itest = (dep.get_detail().get("integration_test") if dep else None) or {}
        fix = result.get("fix_summary") or {}
        repaired_note = ""
        if result.get("repaired"):
            n = len(fix.get("changed_files") or [])
            repaired_note = f";自动修复后端 {n} 个文件(已发布修复版源码)"
        step.set_output(
            output_summary=f"全栈应用已部署并通过健康检查 + 前后端接口联调:前端经 {api_base} 实时调用后端{repaired_note}。",
            reasoning_summary="有序应用部署:建库 → docker build 后端镜像 → 启动长驻容器 → 健康检查 → 契约冒烟 → 拉取前端调用代码+契约做全面接口测试;发现确定性失败则只改后端自动修复(重建+重启+重测,尽力放行)→ 接入反代。修复版后端源码已发布(下载/同步/重部署优先取修复版)。",
            self_check=f"容器 {result.get('container')};镜像={result.get('image')};接口联调={itest.get('gate', 'n/a')}(执行 {itest.get('executed', 0)} 项);修复={fix.get('itest_repaired_rounds', 0)} 轮;预览={preview_url}",
            next_action="在预览中验证前后端联通。",
        )

    return {"status": AgentRunStatus.COMPLETED, "resource_id": project_id}
