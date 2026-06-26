"""
Secondary-development (二次开发) impact analysis workflow.

``code_app_iteration_analysis`` is a lightweight planning run started from a
deployed app in the App Space. It reads the project's requirements / development
flow / shared API contract + the current deployment, judges how far the user's
change reaches (which lanes must re-run, whether the API contract or DB schema is
touched, how risky it is), and drafts a **user-confirmable execution plan**. It
writes NOTHING to the live app — generation + deploy only start after the user
confirms the plan (see ``apps_routes.confirm_iteration``).

The analysis itself uses the shared text provider when one is configured; with no
provider (or an unusable response) it degrades to a deterministic rule-based
analysis so the closed loop still works. The execution plan is always derived
deterministically from the analysis, so it stays consistent with the lanes the
confirm step will actually start.
"""
import json
import logging
import re

from backend.extensions import db
from backend.models.agent import AgentRunStatus
from backend.models.code import CodeProject
from backend.models.code.fullstack import (
    CodeAppIteration,
    CodeDeployment,
    CodeProjectLedger,
    ImpactScope,
    IterationChangeType,
    IterationStatus,
)
from backend.services.prompts import prompt_store

logger = logging.getLogger(__name__)

_MAX_CTX_CHARS = 2400

# Injected verbatim into the prompt's [[OUTPUT_EXAMPLE]] so the model copies the
# exact shape. Kept as a literal (not derived) so it is a stable contract.
_OUTPUT_EXAMPLE = """{
  "change_summary": "新增会员等级与权益页",
  "requirement_change": true,
  "ui_change": true,
  "frontend_change": true,
  "backend_change": true,
  "middleware_change": true,
  "contract_change": true,
  "database_change": true,
  "asset_generation_required": false,
  "risk_level": "high",
  "recommended_lanes": ["frontend", "backend", "middleware"],
  "requires_user_confirmation": true,
  "reasoning": ["新增会员等级需要后端持久化", "需要新增权益查询 API", "需要前端新增会员中心页面"]
}"""

# Embedded fallback for the prompt template (used only if the prompt store AND
# the on-disk default both fail to resolve — belt-and-suspenders).
_FALLBACK_PROMPT = (
    "你是应用二次开发的影响分析专家。已部署应用的拥有者提出变更诉求，请判断影响范围、"
    "风险与要重跑的生成泳道，只输出一个 JSON 对象。\n"
    "变更说明:\n[[INSTRUCTION]]\n变更类型:[[CHANGE_TYPE]]\n需求摘要:\n[[REQUIREMENTS]]\n"
    "开发流程摘要:\n[[FLOW]]\n契约/技术栈:\n[[CONTRACT]]\n部署摘要:\n[[DEPLOYMENT]]\n"
    "严格按此结构输出:\n[[OUTPUT_EXAMPLE]]\n"
)

# Keyword scans driving the deterministic fallback (and risk escalation that even
# the model path can't drop — high-stakes surfaces always force confirmation).
_HIGH_RISK_HINTS = ("登录", "登陆", "鉴权", "认证", "权限", "支付", "付款", "结算", "计费", "账单", "login", "auth", "payment", "billing")
_DB_HINTS = ("数据", "表", "字段", "schema", "migration", "迁移", "索引", "模型", "持久化", "存储", "database", "column", "table")
_API_HINTS = ("api", "接口", "endpoint", "契约", "后端", "服务端", "backend")
_UI_HINTS = ("ui", "样式", "颜色", "配色", "布局", "文案", "页面", "界面", "前端", "frontend", "组件", "按钮", "banner", "首页")
_ASSET_HINTS = ("插画", "图标", "icon", "banner", "背景图", "封面", "海报", "贴纸", "头像", "空状态", "illustration")


def _load_prompt() -> str:
    try:
        text = prompt_store.get("code/iteration_analysis_prompt.txt")
        if text and text.strip():
            return text
    except Exception:  # noqa: BLE001 — store/file both unavailable
        logger.warning("iteration analysis prompt unavailable; using embedded fallback")
    return _FALLBACK_PROMPT


def _clip(text: str | None, limit: int = _MAX_CTX_CHARS) -> str:
    return (text or "")[:limit]


def _extract_json(text: str) -> dict | None:
    """Tolerant single-object JSON parse (code-fence / prefix tolerant)."""
    if not text:
        return None
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL | re.IGNORECASE)
    if fenced:
        cleaned = fenced.group(1).strip()
    if not cleaned.startswith("{"):
        match = re.search(r"(\{.*\})", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(1)
    try:
        value = json.loads(cleaned)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        return None


def _deterministic_analysis(instruction: str, change_type: str) -> dict:
    """Rule-based impact analysis when no model is available / usable.

    Encodes the doc's 影响范围规则 as keyword + change-type heuristics so the
    closed loop still produces a sensible, confirmable plan offline.
    """
    text = (instruction or "").lower()

    def hit(hints) -> bool:
        return any(h in text for h in hints)

    high_risk = hit(_HIGH_RISK_HINTS)
    db_change = hit(_DB_HINTS) or change_type == IterationChangeType.DATA_MODEL
    api_change = hit(_API_HINTS) or change_type in (
        IterationChangeType.NEW_FEATURE,
        IterationChangeType.BACKEND_LOGIC,
    )
    ui_change = hit(_UI_HINTS) or change_type in (
        IterationChangeType.UI_CHANGE,
        IterationChangeType.NEW_FEATURE,
    )
    asset = hit(_ASSET_HINTS)

    if change_type == IterationChangeType.UI_CHANGE and not api_change and not db_change:
        lanes = ["frontend"]
    elif change_type == IterationChangeType.BACKEND_LOGIC and not db_change:
        lanes = ["backend"]
    elif change_type == IterationChangeType.DATA_MODEL or db_change:
        lanes = ["frontend", "backend", "middleware"] if ui_change else ["backend", "middleware"]
    elif change_type == IterationChangeType.NEW_FEATURE:
        lanes = ["frontend", "backend", "middleware"] if db_change else ["frontend", "backend"]
    elif change_type == IterationChangeType.BUG_FIX:
        lanes = ["backend"] if (api_change and not ui_change) else (["frontend"] if ui_change else ["backend"])
    else:  # OTHER — be inclusive but not maximal
        lanes = ["frontend", "backend"] if (ui_change or api_change) else ["backend"]

    contract_change = api_change or "backend" in lanes and change_type == IterationChangeType.NEW_FEATURE
    middleware_change = "middleware" in lanes
    risk = "high" if (high_risk or db_change) else ("medium" if (api_change or len(lanes) >= 2) else "low")
    reasoning = []
    if db_change:
        reasoning.append("涉及数据结构变更，需要后端持久化与数据库迁移")
    if api_change:
        reasoning.append("涉及接口/业务规则变更，需要重跑后端")
    if ui_change:
        reasoning.append("涉及界面/交互调整，需要重跑前端")
    if high_risk:
        reasoning.append("触及登录/支付/权限/计费等高风险面，需用户二次确认")
    if not reasoning:
        reasoning.append("按变更类型与关键词推断的最小影响范围")

    return {
        "change_summary": (instruction or "").strip()[:120] or "应用迭代",
        "requirement_change": change_type in (IterationChangeType.NEW_FEATURE, IterationChangeType.DATA_MODEL),
        "ui_change": ui_change or "frontend" in lanes,
        "frontend_change": "frontend" in lanes,
        "backend_change": "backend" in lanes,
        "middleware_change": middleware_change,
        "contract_change": bool(contract_change),
        "database_change": bool(db_change),
        "asset_generation_required": asset,
        "risk_level": risk,
        "recommended_lanes": lanes,
        "requires_user_confirmation": bool(high_risk or db_change),
        "reasoning": reasoning[:3],
        "_degraded": True,
    }


_VALID_LANES = ("frontend", "backend", "middleware")


def _normalize_analysis(parsed: dict, instruction: str, change_type: str) -> dict:
    """Coerce a model analysis into the contract shape with safe defaults."""
    base = _deterministic_analysis(instruction, change_type)
    out = dict(base)
    out.pop("_degraded", None)
    for key in (
        "change_summary",
        "requirement_change",
        "ui_change",
        "frontend_change",
        "backend_change",
        "middleware_change",
        "contract_change",
        "database_change",
        "asset_generation_required",
        "requires_user_confirmation",
    ):
        if key in parsed:
            out[key] = parsed[key]
    if isinstance(parsed.get("risk_level"), str) and parsed["risk_level"] in ("low", "medium", "high"):
        out["risk_level"] = parsed["risk_level"]
    lanes = parsed.get("recommended_lanes")
    if isinstance(lanes, list):
        ordered = [k for k in _VALID_LANES if k in set(str(x) for x in lanes)]
        if ordered:
            out["recommended_lanes"] = ordered
    reasoning = parsed.get("reasoning")
    if isinstance(reasoning, list) and reasoning:
        out["reasoning"] = [str(r) for r in reasoning][:3]
    elif isinstance(reasoning, str) and reasoning.strip():
        out["reasoning"] = [reasoning.strip()]
    # High-stakes surfaces always force confirmation, regardless of model output.
    if any(h in (instruction or "").lower() for h in _HIGH_RISK_HINTS):
        out["risk_level"] = "high"
        out["requires_user_confirmation"] = True
    # Keep the *_change flags coherent with the resolved lanes.
    out["frontend_change"] = "frontend" in out["recommended_lanes"] or bool(out.get("ui_change"))
    out["backend_change"] = "backend" in out["recommended_lanes"]
    out["middleware_change"] = "middleware" in out["recommended_lanes"]
    return out


def _build_plan(analysis: dict, instruction: str, project_title: str, scope: str) -> dict:
    """Deterministically derive the user-confirmable execution plan.

    Built from the analysis so the steps match exactly the lanes the confirm step
    will start — the plan is never out of sync with what actually runs.
    """
    lanes = analysis.get("recommended_lanes") or ImpactScope.lanes_for(scope)
    steps: list[dict] = []
    if analysis.get("requirement_change"):
        steps.append({
            "lane": "requirements",
            "action": "append_delta",
            "description": "在原需求基线上追加本次变更的需求增量(delta)",
        })
    if analysis.get("contract_change"):
        steps.append({
            "lane": "contract",
            "action": "resynthesize",
            "description": "重新合成共享 OpenAPI 契约以覆盖新增/调整的接口",
        })
    if "middleware" in lanes:
        steps.append({
            "lane": "middleware",
            "action": "generate_migration",
            "description": "根据数据模型变更生成 schema / 迁移 / seed",
        })
    if "backend" in lanes:
        steps.append({
            "lane": "backend",
            "action": "modify",
            "description": "按契约实现/调整后端服务、路由、校验与测试",
        })
    if "frontend" in lanes:
        steps.append({
            "lane": "frontend",
            "action": "modify",
            "description": "修改受影响的页面、组件、路由与 API 调用",
        })
    if analysis.get("asset_generation_required"):
        steps.append({
            "lane": "assets",
            "action": "generate",
            "description": "在前端工程内生成并注入所需图形资源",
        })
    steps.append({
        "lane": "deploy",
        "action": "staging",
        "description": "部署新版本并运行健康检查 / 冒烟 / 集成测试后再发布",
    })

    risks = []
    if analysis.get("database_change"):
        risks.append("涉及数据库 schema 变更，需保持旧数据兼容并展示迁移计划")
    if analysis.get("contract_change"):
        risks.append("API 契约变更，前后端需同步")
    if analysis.get("risk_level") == "high":
        risks.append("高风险变更(登录/支付/权限/计费等)，发布前需充分验证")

    return {
        "title": (analysis.get("change_summary") or instruction or project_title or "应用迭代").strip()[:120],
        "scope": scope,
        "lanes": lanes,
        "steps": steps,
        "risks": risks,
        "requires_confirmation": bool(analysis.get("requires_user_confirmation") or analysis.get("risk_level") == "high"),
    }


def _gather_context(project: CodeProject) -> dict:
    """Pull the bounded context blocks injected into the analysis prompt."""
    ledger = CodeProjectLedger.query.filter_by(project_id=project.id).first()
    contract_summary = ""
    if ledger:
        contract = ledger.get_api_contract()
        tech = contract.get("tech_stack") or {}
        contract_summary = (
            f"技术栈: {json.dumps(tech, ensure_ascii=False)}\n"
            f"接口摘要: {_clip(contract.get('api_summary'), 1200)}"
        )
    deployment = CodeDeployment.query.filter_by(project_id=project.id).first()
    deploy_summary = ""
    if deployment:
        deploy_summary = (
            f"部署状态: {deployment.status} / 健康: {deployment.health or 'unknown'} / "
            f"API: {deployment.api_base_path or ''}"
        )
    return {
        "requirements": _clip(project.requirements_doc),
        "flow": _clip(project.development_flow),
        "contract": contract_summary or "(尚无契约)",
        "deployment": deploy_summary or "(尚未部署)",
    }


def _analyze_with_model(instruction: str, change_type: str, ctx: dict, tracer) -> dict | None:
    """Run the model analysis; return a parsed dict or None to fall back."""
    from backend.services.ai import get_text_provider

    provider = get_text_provider(force_new=True)
    if not provider or not provider.is_configured():
        return None
    prompt = (
        _load_prompt()
        .replace("[[INSTRUCTION]]", instruction)
        .replace("[[CHANGE_TYPE]]", change_type)
        .replace("[[REQUIREMENTS]]", ctx["requirements"])
        .replace("[[FLOW]]", ctx["flow"])
        .replace("[[CONTRACT]]", ctx["contract"])
        .replace("[[DEPLOYMENT]]", ctx["deployment"])
        .replace("[[OUTPUT_EXAMPLE]]", _OUTPUT_EXAMPLE)
    )
    try:
        result = provider.generate_text(prompt)
    except Exception as error:  # noqa: BLE001
        logger.warning("iteration analysis model raised: %s", error)
        if tracer:
            tracer(prompt=prompt, text=None, success=False, error=str(error))
        return None
    if tracer:
        tracer(prompt=prompt, text=result.text, success=result.success, error=result.error)
    if not result.success:
        return None
    return _extract_json(result.text)


def run_code_app_iteration_analysis_workflow(ctx, recorder) -> dict:
    """Entry point for the ``code_app_iteration_analysis`` workflow."""
    iteration_id = (ctx.config or {}).get("iteration_id")
    iteration = db.session.get(CodeAppIteration, iteration_id) if iteration_id else None
    project = db.session.get(CodeProject, ctx.resource_id) if ctx.resource_id else None

    if not iteration or not project or iteration.project_id != project.id:
        # Nothing to analyse — fail loudly so the run is marked failed.
        raise ValueError("迭代记录或项目缺失，无法进行影响分析")
    # Ownership gate: the run carries the caller's identity (ctx.user_id). Refuse
    # to touch an iteration / project that isn't theirs, so invoking this workflow
    # directly via POST /api/agent/runs with someone else's ids leaks nothing and
    # mutates nothing. Checked BEFORE any status change / context gather / artifact.
    if iteration.user_id != ctx.user_id or project.user_id != ctx.user_id:
        raise ValueError("无权访问该迭代或项目")

    instruction = iteration.instruction or ""
    change_type = iteration.change_type or IterationChangeType.OTHER

    try:
        iteration.status = IterationStatus.ANALYZING
        db.session.commit()
        context = _gather_context(project)

        # Step 1 — impact analysis (model when available, else deterministic).
        with recorder.step(
            "iteration_analyst",
            "影响分析",
            "planner",
            0,
            input_summary=instruction[:200],
        ) as step:
            parsed = _analyze_with_model(instruction, change_type, context, step.model_tracer())
            if parsed:
                analysis = _normalize_analysis(parsed, instruction, change_type)
                degraded = False
            else:
                analysis = _deterministic_analysis(instruction, change_type)
                degraded = bool(analysis.pop("_degraded", False))
            iteration.set_analysis(analysis)
            db.session.commit()
            step.add_artifact(
                "json",
                "影响分析",
                content_json=analysis,
                domain_ref_type="code_app_iteration",
                domain_ref_id=iteration.id,
            )
            step.set_output(
                output_summary=analysis.get("change_summary"),
                reasoning_summary="；".join(analysis.get("reasoning") or []),
                self_check=("规则兜底分析" if degraded else "模型分析"),
            )

        # Resolve the impact scope: respect a user override set at create time,
        # else derive from the analysis's recommended lanes.
        scope = iteration.impact_scope or ImpactScope.from_lanes(analysis.get("recommended_lanes") or [])

        # Step 2 — execution plan (deterministic, derived from the analysis).
        with recorder.step("iteration_planner", "执行计划", "planner", 1) as step:
            plan = _build_plan(analysis, instruction, project.title, scope)
            iteration.set_plan(plan)
            iteration.impact_scope = scope
            iteration.status = IterationStatus.AWAITING_PLAN_APPROVAL
            db.session.commit()
            step.add_artifact(
                "json",
                "执行计划",
                content_json=plan,
                domain_ref_type="code_app_iteration",
                domain_ref_id=iteration.id,
            )
            step.set_output(
                output_summary=plan.get("title"),
                reasoning_summary=f"影响范围: {scope}；共 {len(plan.get('steps') or [])} 步",
            )
    except Exception as exc:  # noqa: BLE001 — record on the iteration, then re-raise
        logger.error("iteration analysis failed for %s: %s", iteration_id, exc, exc_info=True)
        db.session.rollback()
        fresh = db.session.get(CodeAppIteration, iteration_id)
        if fresh and fresh.status in IterationStatus.ACTIVE:
            fresh.status = IterationStatus.FAILED
            fresh.error_message = str(exc)
            db.session.commit()
        raise

    return {"status": AgentRunStatus.COMPLETED, "resource_id": project.id}
