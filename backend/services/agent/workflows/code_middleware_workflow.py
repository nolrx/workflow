"""
Code domain workflow — ``code_middleware_provisioning``.

Derives the data/infra layer the generated backend needs from the SHARED
middleware manifest + the flow's ``## 数据设计`` section: a portable ``init.sql``
(DDL), optional seed data, and an entity/index spec. Runs CONCURRENTLY with the
frontend and backend generation runs for the same project.

This run only GENERATES the data layer; the actual namespace creation
(``CREATE DATABASE app_<pid>`` + redis prefix) and SQL application happen at
deploy time (so the three generation runs stay independent and side-effect-free).
The generated backend self-migrates where possible; ``init.sql`` is the
deploy-time fallback.

Steps: ``mw_planner`` -> ``mw_provision`` -> ``mw_publish`` (3 counted steps).
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
from backend.services.agent.context_ledger import ContextLedger, seed_from_inputs
from backend.services.code import middleware_service
from backend.services.code.fullstack import contract_service

logger = logging.getLogger(__name__)

TOTAL_STEPS = 3


def _load_shared(project: CodeProject, user_id: str, team_id):
    row = contract_service.get_ledger(project.id)
    if row is None or row.contract_status != "ready":
        row = contract_service.ensure_contract(project, user_id, team_id)
    contract = row.get_api_contract()
    manifest = row.get_middleware_manifest()
    ledger = ContextLedger.load(row.get_shared_ledger())
    if ledger.is_empty():
        ledger = seed_from_inputs(
            project.requirement_input, project.title, project.get_selected_style_ids()
        )
    return contract, manifest, ledger


def run_code_middleware_workflow(ctx, recorder) -> dict:
    """Generate + publish the data/infra layer for the generated backend."""
    completed = 0
    ledger = ContextLedger.empty()
    contract: dict = {}
    manifest: dict = {}

    def progress(current_step: str) -> None:
        run = db.session.get(AgentRun, ctx.run_id)
        if run:
            run.set_progress(
                {
                    "total_steps": TOTAL_STEPS,
                    "completed_steps": completed,
                    "failed_steps": 0,
                    "current_step": current_step,
                }
            )
            db.session.commit()
        recorder.emit(
            AgentEventType.PROGRESS,
            message=f"进度 {completed}/{TOTAL_STEPS}",
            payload={"completed": completed, "total": TOTAL_STEPS, "current": current_step},
        )

    def cancel_result(project_id) -> dict:
        recorder.emit(
            AgentEventType.WARNING,
            level=AgentEventLevel.WARNING,
            message="收到取消请求，停止后续步骤",
        )
        return {"status": AgentRunStatus.CANCELLED, "resource_id": project_id}

    # --- Step 1: Planner -----------------------------------------------------
    with recorder.step(
        "mw_planner", "中间件规划 Agent", "planner", 1, input_summary="校验项目并载入共享中间件清单"
    ) as step:
        if not ctx.resource_id:
            raise ValueError("缺少 resource_id：中间件生成需要一个已有的 Code 项目")
        project = CodeProject.query.filter_by(id=ctx.resource_id, user_id=ctx.user_id).first()
        if not project:
            raise ValueError("项目不存在或无权访问")
        if not project.development_flow:
            raise ValueError("项目尚未完成开发流程，无法推导中间件")
        project_id = project.id

        contract, manifest, ledger = _load_shared(project, ctx.user_id, ctx.team_id)
        mw_run = db.session.get(AgentRun, ctx.run_id)
        mw_run.set_context_ledger(ledger.to_dict())
        db.session.commit()

        datastores = manifest.get("datastores") or []
        recorder.emit(
            AgentEventType.PROGRESS,
            step_id=step.id,
            message=f"中间件需求:{len(datastores)} 个数据存储"
            + ("，含缓存" if manifest.get("cache") else ""),
            payload={"manifest": manifest},
        )
        step.set_context(snapshot={"injected_text": "", "ledger": ledger.to_dict()})
        step.set_output(
            output_summary=f"已载入中间件清单:{', '.join(d.get('type', '') for d in datastores) or '默认 postgres'}。",
            reasoning_summary="从共享契约的中间件清单与开发流程「数据设计」推导后端所需的数据/缓存层。",
            decision_notes="复用平台共享 postgres/redis,按项目命名空间隔离(建库/键前缀);本步只生成数据层,实建库在部署阶段。",
            self_check=f"数据存储 {len(datastores)} 项;缓存 {'有' if manifest.get('cache') else '无'}。",
            next_action="生成初始化 SQL / 迁移与种子数据。",
        )
    completed += 1
    progress("provision")

    # --- Step 2: Provision (generate data layer) -----------------------------
    if ctx.is_cancelled():
        return cancel_result(project_id)
    data_layer: dict = {}
    with recorder.step(
        "mw_provision",
        "中间件生成 Agent",
        "generator",
        2,
        input_summary="生成初始化 SQL / 迁移 / 种子数据",
    ) as step:
        project = db.session.get(CodeProject, project_id)
        contract_summary = contract_service.render_contract_for_prompt(contract, max_chars=6000)
        step.model_provider = "text"
        db.session.commit()
        data_layer = middleware_service.generate_data_layer(project, manifest, contract_summary)
        n_entities = len(data_layer.get("entities") or [])
        has_sql = bool((data_layer.get("init_sql") or "").strip())
        if data_layer.get("_degraded"):
            recorder.emit(
                AgentEventType.WARNING,
                level=AgentEventLevel.WARNING,
                step_id=step.id,
                message="未配置文本模型:数据层为确定性回退(建表交给后端自带迁移)。",
            )
        recorder.emit(
            AgentEventType.MODEL_RESPONSE,
            step_id=step.id,
            message="数据层生成完成",
            payload={
                "entities": n_entities,
                "has_init_sql": has_sql,
                "summary": data_layer.get("summary"),
            },
        )
        step.set_output(
            output_summary=f"已生成数据层:{n_entities} 个实体,{'含' if has_sql else '无'}初始化 SQL。{data_layer.get('summary', '')}".strip(),
            reasoning_summary="按清单与数据设计产出可移植 DDL 与种子;部署时作为非自迁移后端的兜底初始化。",
            self_check=f"实体 {n_entities};init.sql {'有' if has_sql else '无'};seed {'有' if (data_layer.get('seed_sql') or '').strip() else '无'}。",
            next_action="发布中间件清单与 SQL 产物。",
        )
    completed += 1
    progress("publish")

    # --- Step 3: Publish -----------------------------------------------------
    with recorder.step(
        "mw_publish", "发布 Agent", "publisher", 3, input_summary="保存中间件清单与初始化产物"
    ) as step:
        init_sql = data_layer.get("init_sql") or ""
        seed_sql = data_layer.get("seed_sql") or ""
        if init_sql.strip() or seed_sql.strip():
            combined = (init_sql + ("\n\n-- seed\n" + seed_sql if seed_sql.strip() else "")).strip()
            step.add_artifact(
                AgentArtifactType.TEXT,
                "初始化 SQL（init.sql）",
                filename="init.sql",
                mime_type="text/plain; charset=utf-8",
                write_file=True,
                content_text=combined,
                domain_ref_type="code_middleware_sql",
                domain_ref_id=project_id,
            )
        step.add_artifact(
            AgentArtifactType.JSON,
            "中间件清单与数据层",
            content_json={
                "manifest": manifest,
                "entities": data_layer.get("entities") or [],
                "init_sql": init_sql,
                "seed_sql": seed_sql,
                "summary": data_layer.get("summary"),
                "notes": data_layer.get("notes"),
                "delivery": "middleware-provisioning",
            },
            filename="middleware_meta.json",
            domain_ref_type="code_middleware_meta",
            domain_ref_id=project_id,
        )
        step.set_context(snapshot={"injected_text": "", "ledger": ledger.to_dict()})
        step.set_output(
            output_summary="中间件清单与数据层已发布:部署阶段据此建库并(必要时)应用初始化 SQL。",
            reasoning_summary="把清单与 SQL 作为 artifact 落库;部署 run 读取它创建项目专属命名空间。",
            self_check=f"清单数据存储 {len(manifest.get('datastores') or [])};SQL {'有' if init_sql.strip() else '无'}。",
            next_action="待三端就绪后触发应用部署。",
        )
    completed += 1
    progress("done")

    return {"status": AgentRunStatus.COMPLETED, "resource_id": project_id}
