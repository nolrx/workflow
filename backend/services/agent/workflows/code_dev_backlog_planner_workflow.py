"""
Dev Mode backlog-planner workflow (``code_dev_backlog_planner``, P1).

One bounded run that turns the project docs + session ledger + current task
board + the user's planning instruction into a NORMALIZED, user-confirmable
task draft (``CodeDevTaskPlan``, contract ``dev-backlog-plan.v1``):

  1. planner_prepare   — validate project/session/plan, build the planning
     context, pin the input fingerprint (stale detection at apply time).
  2. planner_generate  — one text-model call; degrades to the deterministic
     ledger-derived fallback when the provider is missing/failing.
  3. planner_normalize — parse (fence-tolerant), clamp, dedupe, filter lanes,
     resolve/break dependencies. The normalizer's warnings are the user-facing
     record of every change it made to the raw model output.
  4. planner_publish   — persist the plan (status ``draft``), attach the
     ``dev_backlog_plan.json`` artifact, emit the plan snapshot.

The plan NEVER writes the task board — apply is a separate route through the
same guarded bulk-write path as ``tasks/bulk``. Comments in English to match
the Code/core convention.
"""
import logging

from backend.extensions import db
from backend.models.agent import AgentEventLevel, AgentEventType, AgentRunStatus
from backend.models.code import CodeProject
from backend.models.code.fullstack import (
    CodeDevSession,
    CodeDevTaskPlan,
    DevTaskPlanStatus,
)
from backend.services.code import dev_backlog_planner_service as planner
from backend.services.code import dev_sprint_service

logger = logging.getLogger(__name__)


def run_code_dev_backlog_planner_workflow(ctx, recorder) -> dict:
    """Entry point for ``code_dev_backlog_planner`` — produce one task draft."""
    cfg = ctx.config or {}
    session_id = cfg.get("session_id")
    plan_id = cfg.get("plan_id")
    project_id = ctx.resource_id

    def _fail_plan(message: str) -> None:
        plan_row = db.session.get(CodeDevTaskPlan, plan_id) if plan_id else None
        if plan_row and plan_row.status not in DevTaskPlanStatus.TERMINAL:
            plan_row.status = DevTaskPlanStatus.FAILED
            plan_row.error_message = message[:1000]
            db.session.commit()

    try:
        # --- Step 1: prepare ---------------------------------------------------
        with recorder.step(
            "planner_prepare", "规划准备", "planner", 1,
            input_summary=(cfg.get("instruction") or "按项目文档全量拆分")[:200],
        ) as step:
            project = CodeProject.query.filter_by(id=project_id, user_id=ctx.user_id).first()
            if not project:
                raise ValueError("项目不存在或无权访问")
            session = db.session.get(CodeDevSession, session_id) if session_id else None
            if not session or session.project_id != project.id or session.user_id != ctx.user_id:
                raise ValueError("开发会话不存在或无权访问")
            plan_row = db.session.get(CodeDevTaskPlan, plan_id) if plan_id else None
            if not plan_row or plan_row.session_id != session.id:
                raise ValueError("任务计划不存在或不属于本开发会话")
            if session.lane != "frontend":
                raise ValueError("P1 阶段任务规划仅支持前端开发会话")

            context = planner.build_planner_context(
                project, session,
                target_lanes=cfg.get("target_lanes") or None,
                include_assets=cfg.get("include_assets", True),
                max_tasks=cfg.get("max_tasks") or planner.DEFAULT_MAX_TASKS,
                instruction=cfg.get("instruction") or "",
            )
            fingerprint = planner.input_fingerprint(context)
            plan_row.input_fingerprint = fingerprint
            plan_row.set_target_lanes(context["target_lanes"])
            if plan_row.run_id != ctx.run_id:
                plan_row.run_id = ctx.run_id
            db.session.commit()
            step.set_output(
                output_summary=(
                    f"上下文就绪:任务板 {len(context['board'])} 项,目标 lane "
                    f"{'/'.join(context['target_lanes'])},上限 {context['max_tasks']} 任务。"
                ),
                self_check=f"输入指纹 {fingerprint[:12]}…(apply 时校验漂移)。",
            )

        # --- Step 2: generate (fan-out: per-FR concurrent split on the fast model) -
        raw_plan = None
        degraded = False
        with recorder.step(
            "planner_generate", "任务拆分 Agent", "generator", 2,
            input_summary="逐需求并发细拆(fast 模型) → 任务草案",
        ) as step:
            last_progress = [0]

            def _on_progress(done: int, total: int, ok: int, fb: int) -> None:
                # Throttle: emit at most every ~25% so the timeline isn't spammed.
                if done == total or done - last_progress[0] >= max(1, total // 4):
                    last_progress[0] = done
                    recorder.emit(
                        AgentEventType.PROGRESS, step_id=step.id,
                        message=f"逐需求细拆 {done}/{total}(细拆 {ok} · 退回粗任务 {fb})",
                        payload={"done": done, "total": total, "ok": ok, "fallback": fb},
                    )

            try:
                raw_plan, mode = planner.build_raw_plan(
                    context,
                    on_model_call=step.model_tracer() if hasattr(step, "model_tracer") else None,
                    on_progress=_on_progress,
                )
            except Exception as exc:  # noqa: BLE001 — never sink the run; deterministic plan
                logger.warning("planner build_raw_plan raised: %s", exc)
                raw_plan, mode = planner.deterministic_fallback(context), "fallback"
            degraded = mode == "fallback"
            if degraded:
                recorder.emit(
                    AgentEventType.WARNING, level=AgentEventLevel.WARNING, step_id=step.id,
                    message="模型细拆不可用,已按共识账本 FR/NFR 生成保守任务草案。",
                )
            step.set_output(
                output_summary={
                    "fanout": f"逐需求细拆完成(原始 {len(raw_plan.get('tasks') or [])} 项)。",
                    "fanout_partial": f"逐需求细拆完成,部分退回粗任务(原始 {len(raw_plan.get('tasks') or [])} 项)。",
                    "model": f"模型返回任务草案(原始 {len(raw_plan.get('tasks') or [])} 项)。",
                    "fallback": "已按共识账本生成保守回退草案。",
                }.get(mode, f"任务草案(原始 {len(raw_plan.get('tasks') or [])} 项)。"),
            )

        # --- Step 3: normalize ----------------------------------------------------
        with recorder.step(
            "planner_normalize", "草案规范化", "reviewer", 3,
            input_summary="裁剪/去重/lane 过滤/依赖解析与断环",
        ) as step:
            existing = dev_sprint_service.session_tasks(session.id)
            plan, warnings = planner.normalize_plan(
                raw_plan, existing_tasks=existing, max_tasks=context["max_tasks"],
            )
            if degraded:
                warnings.insert(0, "degraded:fallback")
            plan["request"] = {
                "instruction": context["instruction"],
                "include_assets": context["include_assets"],
                "max_tasks": context["max_tasks"],
            }
            if not plan["tasks"]:
                raise RuntimeError("规划结果为空:模型与回退方案均未产出任何合法任务")
            for w in warnings[:10]:
                recorder.emit(
                    AgentEventType.WARNING, level=AgentEventLevel.WARNING,
                    step_id=step.id, message=f"规范化:{w}",
                )
            step.set_output(
                output_summary=f"规范化完成:{len(plan['tasks'])} 项任务,{len(warnings)} 条警告。",
                self_check="feature_id 唯一;依赖无环;仅 frontend/asset lane。",
            )

        # --- Step 4: publish --------------------------------------------------------
        with recorder.step(
            "planner_publish", "草案发布", "publisher", 4,
            input_summary="写入计划草案,等待用户确认",
        ) as step:
            plan_row = db.session.get(CodeDevTaskPlan, plan_id)
            # A reclaim (create_task_plan) may have failed this plan while its model
            # call hung — don't resurrect a superseded plan to DRAFT.
            if plan_row.status != DevTaskPlanStatus.PLANNING:
                recorder.emit(
                    AgentEventType.WARNING, level=AgentEventLevel.WARNING, step_id=step.id,
                    message="该计划已被新的规划请求取代,发布跳过。",
                )
                return {"status": AgentRunStatus.COMPLETED, "resource_id": project_id,
                        "plan_id": plan_id}
            plan_row.set_plan(plan)
            plan_row.set_warnings(warnings)
            plan_row.status = DevTaskPlanStatus.DRAFT
            db.session.commit()
            step.add_artifact(
                "json", "任务草案(dev-backlog-plan.v1)",
                content_json=plan, filename="dev_backlog_plan.json",
                domain_ref_type="code_dev_task_plan", domain_ref_id=plan_row.id,
            )
            recorder.emit(
                AgentEventType.PROGRESS,
                step_id=step.id,
                message=f"任务草案就绪:{len(plan['tasks'])} 项,等待确认后应用到任务板",
                payload={"plan": plan_row.to_dict()},
            )
            step.set_output(output_summary=f"草案已发布({len(plan['tasks'])} 项任务)。")

        return {
            "status": AgentRunStatus.COMPLETED,
            "resource_id": project_id,
            "plan_id": plan_id,
        }
    except Exception as exc:
        _fail_plan(str(exc))
        raise
