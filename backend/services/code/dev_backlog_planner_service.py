"""
Dev Mode backlog planner (P1) — turns project docs + ledger + the current task
board + a user goal into a NORMALIZED, user-confirmable task draft
(``dev-backlog-plan.v1``) that applies through the same guarded bulk-write path
as ``tasks/bulk``.

Hard rules encoded here (not left to the model):

  * the plan never touches the board directly — apply is a separate, explicit,
    fingerprint-guarded step;
  * every task must be executable by ONE ``code_dev_turn`` (the prompt demands
    it; the normalizer enforces the field-level caps);
  * P1 only produces ``frontend``/``asset`` lanes — anything else is filtered
    into a warning, never written;
  * dependency references must resolve (existing DONE tasks or in-plan ids) and
    must be acyclic — unknown deps are dropped, cycles are deterministically
    broken, both with warnings, so a bad plan can never strand the sprint.

Comments in English to match the Code/core convention.
"""
import hashlib
import json
import logging
import os
import re
from datetime import datetime

from backend.extensions import db
from backend.models.code.fullstack import (
    CodeDevSprint,
    CodeDevTaskPlan,
    DevSprintStatus,
    DevTaskPlanStatus,
    DevTaskSource,
    DevTaskStatus,
)
from backend.services.code import asset_lane, dev_sprint_service
from backend.services.prompts import prompt_store

logger = logging.getLogger(__name__)

PLAN_VERSION = "dev-backlog-plan.v1"
# P1 only schedules what the frontend session can execute.
ALLOWED_LANES = {"frontend", "asset"}
MAX_PLAN_TASKS = 200
DEFAULT_MAX_TASKS = 80

_PROMPT_KEY = "code/dev_backlog_planner_prompt.txt"

# The JSON contract shown to the model ([[OUTPUT_SCHEMA]]); kept in code so the
# prompt file stays declarative and the schema is versioned with the parser.
OUTPUT_SCHEMA = """{
  "version": "dev-backlog-plan.v1",
  "summary": "本轮任务拆分摘要(1-2 句)",
  "assumptions": ["必要假设,可为空数组"],
  "target_lanes": ["frontend", "asset"],
  "tasks": [
    {
      "feature_id": "FR1.T1",
      "parent_feature_id": "FR1",
      "lane": "frontend | asset",
      "category": "functional | nonfunctional | asset | chore | test",
      "title": "一句话任务标题",
      "description": "补充说明,可省略",
      "acceptance_criteria": ["1~8 条,每条可机器/评审验收"],
      "depends_on": ["只能引用已完成任务或本计划内的 feature_id"],
      "resource_spec": {"skill": "image-assets", "outputs": [{"path": "src/assets/xx.png", "size": "1536x1024", "prompt": "英文图像提示词", "required": true}]},
      "priority": 20,
      "max_retries": 2,
      "planner_meta": {"risk": "low|medium|high", "estimated_turns": 1, "files_hint": ["src/pages/X.tsx"]}
    }
  ],
  "warnings": ["模型自报的注意事项,可为空数组"]
}"""


def _fill(template: str, **values) -> str:
    out = template
    for key, value in values.items():
        out = out.replace(f"[[{key}]]", value if value is not None else "")
    return out


# --- context / fingerprint -------------------------------------------------------
def build_planner_context(
    project,
    session,
    *,
    target_lanes: list[str] | None = None,
    include_assets: bool = True,
    max_tasks: int = DEFAULT_MAX_TASKS,
    instruction: str = "",
) -> dict:
    """Everything the planner (and the fingerprint) needs, pre-truncated."""
    lanes = [ln for ln in (target_lanes or ["frontend", "asset"]) if ln in ALLOWED_LANES]
    if not lanes:
        lanes = ["frontend"]
    if not include_assets:
        lanes = [ln for ln in lanes if ln != "asset"]
    tasks = dev_sprint_service.session_tasks(session.id)
    board = [
        {
            "feature_id": t.feature_id,
            "status": t.status,
            "title": t.title,
            "acceptance_criteria": t.get_acceptance_criteria(),
            "depends_on": t.get_depends_on(),
        }
        for t in tasks
    ]
    active_sprint = (
        CodeDevSprint.query.filter_by(session_id=session.id)
        .filter(CodeDevSprint.status.in_(list(DevSprintStatus.ACTIVE)))
        .first()
    )
    return {
        "project_id": project.id,
        "session_id": session.id,
        # Kept compact so a reasoning model has token headroom to actually emit the
        # plan JSON (an oversized prompt makes it burn its budget reasoning and time
        # out). The FR/NFR list — the planner's real input — is carried compactly by
        # EXISTING_BOARD (the seeded ledger features), so the full docs can be short.
        "requirements_doc": (project.requirements_doc or "")[:6_000],
        "development_flow": (project.development_flow or "")[:3_000],
        "style_prompt": "",  # visual style is irrelevant to task breakdown
        "shared_ledger_raw": session.shared_ledger_raw or "",
        "board": board,
        "done_feature_ids": sorted(
            t.feature_id for t in tasks if t.feature_id and t.status == DevTaskStatus.DONE
        ),
        "target_lanes": sorted(lanes),
        "include_assets": bool(include_assets and "asset" in lanes),
        "max_tasks": max(1, min(MAX_PLAN_TASKS, int(max_tasks or DEFAULT_MAX_TASKS))),
        "instruction": (instruction or "").strip()[:2_000],
        "sprint_active": bool(active_sprint),
    }


def input_fingerprint(context: dict) -> str:
    """Stable hash over the inputs the plan was derived from (stale detection)."""
    basis = {
        "requirements_doc": context.get("requirements_doc") or "",
        "development_flow": context.get("development_flow") or "",
        "style_prompt": context.get("style_prompt") or "",
        "shared_ledger_raw": context.get("shared_ledger_raw") or "",
        "board": [
            [
                b.get("feature_id"), b.get("status"), b.get("title"),
                b.get("acceptance_criteria"), b.get("depends_on"),
            ]
            for b in context.get("board") or []
        ],
        "target_lanes": context.get("target_lanes") or [],
        "include_assets": bool(context.get("include_assets")),
        "instruction": context.get("instruction") or "",
    }
    payload = json.dumps(basis, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- model call / parsing ----------------------------------------------------------
def render_planner_prompt(context: dict) -> str:
    template = prompt_store.get(_PROMPT_KEY)
    board_lines = []
    for b in context.get("board") or []:
        deps = f" deps={','.join(b['depends_on'])}" if b.get("depends_on") else ""
        board_lines.append(
            f"- [{b.get('feature_id') or '-'}] ({b.get('status')}) {b.get('title')}{deps}"
        )
    project_context = "\n\n".join(p for p in [
        f"# 需求文档(节选)\n{context.get('requirements_doc') or '(空)'}",
        f"# 开发流程(节选)\n{context['development_flow']}" if context.get("development_flow") else "",
        f"# 风格文档(节选)\n{context['style_prompt']}" if context.get("style_prompt") else "",
    ] if p)
    return _fill(
        template,
        PROJECT_CONTEXT=project_context,
        EXISTING_BOARD="\n".join(board_lines) or "(任务板为空)",
        TARGET_LANES=", ".join(context.get("target_lanes") or ["frontend"]),
        USER_PLANNING_INSTRUCTION=context.get("instruction") or "(无额外指令,按文档全量拆分)",
        MAX_TASKS=str(context.get("max_tasks") or DEFAULT_MAX_TASKS),
        OUTPUT_SCHEMA=OUTPUT_SCHEMA,
    )


# The planner must fail FAST to the deterministic fallback: a reasoning model
# (deepseek) on a large prompt can burn its whole token budget reasoning and time
# out, and the default provider retries twice (~3×120s ≈ 6 min) — which strands the
# plan in `planning` for minutes. One attempt with a tight read-timeout caps that.
_PLANNER_MODEL_TIMEOUT = float(os.getenv("CODE_DEV_PLANNER_TIMEOUT", "100"))

# --- fan-out decomposition (per-FR concurrent split on the FAST model) ---------
# A single call asking a model to decompose the WHOLE backlog is too big — it times
# out / burns its token budget. Instead, split ONE requirement per call (small
# prompt, small output) on the fast model tier, run them CONCURRENTLY (mirrors
# _verify_support.run_reviewers — pure model calls, no DB, thread-safe), then merge.
# A per-FR failure degrades to that FR's coarse task, so the plan is never empty.
_FANOUT_ENABLED = os.getenv("CODE_DEV_PLANNER_FANOUT", "1").strip().lower() not in ("0", "", "false", "no")
_FANOUT_MAX_WORKERS = int(os.getenv("CODE_DEV_PLANNER_FANOUT_WORKERS", "6"))
_FANOUT_FR_TIMEOUT = float(os.getenv("CODE_DEV_PLANNER_FR_TIMEOUT", "60"))
_FANOUT_MAX_SUB = int(os.getenv("CODE_DEV_PLANNER_FR_MAX_SUB", "4"))
_FANOUT_MAX_FRS = int(os.getenv("CODE_DEV_PLANNER_FANOUT_MAX_FRS", "40"))
# Auto-split coarse ledger-seed tasks (one whole FR, no granular AC) into small
# winnable sub-tasks BEFORE a sprint runs. A monolithic FR is unwinnable in a
# single turn under the adversarial reviewer (verify->repair->retry->blocked), so
# this is on by default. Reuses the per-FR fan-out split above.
_AUTO_DECOMPOSE_ENABLED = os.getenv(
    "CODE_DEV_SPRINT_AUTO_DECOMPOSE", "1"
).strip().lower() not in ("0", "", "false", "no")


def fanout_enabled() -> bool:
    return _FANOUT_ENABLED


def auto_decompose_enabled() -> bool:
    return _AUTO_DECOMPOSE_ENABLED


def call_planner_model(prompt: str, on_model_call=None):
    """One text-model call; returns the provider result object (success flag inside).

    Uses a fresh provider (``force_new=True``) with NO retries and a tight timeout so
    a hung gateway falls to the deterministic fallback in seconds, not minutes.
    """
    from backend.services.ai.factory import get_text_provider

    # Task decomposition is fast/structured work → the fast model tier (flash); a
    # reasoning model over-thinks a large structured request and times out.
    provider = get_text_provider(force_new=True, role="fast")
    # Fail-fast budget — only mutate the fresh (non-cached) instance.
    try:
        if hasattr(provider, "timeout"):
            provider.timeout = _PLANNER_MODEL_TIMEOUT
            provider.max_retries = 0
            provider._client = None
            provider._configure()
    except Exception:  # noqa: BLE001 — keep the default budget if override fails
        logger.warning("planner provider fast-fail override failed", exc_info=True)
    result = provider.generate_text(prompt)
    if on_model_call:
        try:
            on_model_call(
                prompt=prompt, text=getattr(result, "text", None),
                success=getattr(result, "success", False),
                error=getattr(result, "error", None),
                provider=getattr(provider, "provider_name", provider.__class__.__name__),
                model=getattr(result, "model", None) or getattr(provider, "model", None),
            )
        except Exception:  # noqa: BLE001 — tracing must never sink the plan
            logger.warning("planner model trace failed", exc_info=True)
    return result


def parse_plan_json(text: str) -> dict | None:
    """Parse the model output into a dict; tolerates markdown fences / prose."""
    if not text:
        return None
    candidates = [text.strip()]
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        candidates.insert(0, fence.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for cand in candidates:
        try:
            data = json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


# --- normalization -------------------------------------------------------------------
def _break_cycles(tasks: list[dict]) -> list[str]:
    """Deterministically drop dependency edges that close a cycle (in plan order).
    Returns warnings for every dropped edge. Cycles must NEVER reach the DB."""
    warnings: list[str] = []
    ids = [t["feature_id"] for t in tasks]
    idx = {fid: i for i, fid in enumerate(ids)}
    deps = {t["feature_id"]: [d for d in t["depends_on"] if d in idx] for t in tasks}

    # Iterative DFS cycle detection; on a back-edge, drop it.
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {fid: WHITE for fid in ids}

    def visit(fid: str) -> None:
        color[fid] = GRAY
        for dep in list(deps[fid]):
            if color[dep] == GRAY:  # back-edge → cycle
                deps[fid].remove(dep)
                warnings.append(f"依赖环:已断开 {fid} -> {dep}")
            elif color[dep] == WHITE:
                visit(dep)
        color[fid] = BLACK

    for fid in ids:
        if color[fid] == WHITE:
            visit(fid)
    for t in tasks:
        in_plan = set(deps[t["feature_id"]])
        t["depends_on"] = [d for d in t["depends_on"] if d not in idx or d in in_plan]
    return warnings


def normalize_plan(
    raw: dict | None,
    *,
    existing_tasks: list | None = None,
    max_tasks: int = DEFAULT_MAX_TASKS,
) -> tuple[dict, list[str]]:
    """Clamp/dedupe/validate a raw model plan into ``dev-backlog-plan.v1``.

    Never raises on bad content — everything invalid is dropped/filtered with a
    warning so the user sees exactly what the normalizer did.
    """
    warnings: list[str] = []
    raw = raw if isinstance(raw, dict) else {}
    existing = {t.feature_id: t for t in (existing_tasks or []) if t.feature_id}
    protected = {
        fid for fid, t in existing.items()
        if t.status == DevTaskStatus.DONE or t.status in DevTaskStatus.ACTIVE
    }
    done_ids = {fid for fid, t in existing.items() if t.status == DevTaskStatus.DONE}

    cap = max(1, min(MAX_PLAN_TASKS, int(max_tasks or DEFAULT_MAX_TASKS)))
    raw_tasks = raw.get("tasks") if isinstance(raw.get("tasks"), list) else []
    if len(raw_tasks) > cap:
        warnings.append(f"任务数超上限,已截断到前 {cap} 个(原 {len(raw_tasks)})")
        raw_tasks = raw_tasks[:cap]

    tasks: list[dict] = []
    seen: set[str] = set()
    auto_seq = 0
    for item in raw_tasks:
        if not isinstance(item, dict):
            warnings.append("忽略非法任务项(非对象)")
            continue
        title = str(item.get("title") or "").strip()[:300]
        if not title:
            warnings.append("忽略无标题任务")
            continue
        category = dev_sprint_service.normalize_category(item.get("category"))
        lane = dev_sprint_service.normalize_lane(item.get("lane"))
        if category == "asset" and lane is None:
            lane = "asset"
        if lane is None:
            lane = "frontend"
        if lane not in ALLOWED_LANES:
            warnings.append(f"过滤不支持的 lane={lane}:{title[:40]}(P1 仅 frontend/asset)")
            continue

        fid = str(item.get("feature_id") or "").strip()[:60]
        if not fid:
            auto_seq += 1
            fid = f"AUTO.T{auto_seq}"
            warnings.append(f"任务缺 feature_id,已生成 {fid}:{title[:40]}")
        if fid in seen:
            warnings.append(f"重复 feature_id={fid},已忽略后一个")
            continue
        seen.add(fid)
        if fid in protected:
            warnings.append(f"feature_id={fid} 与已完成/执行中任务同名,apply 时不会覆盖")

        criteria = item.get("acceptance_criteria")
        criteria = [str(c).strip()[:500] for c in criteria[:20] if str(c).strip()] \
            if isinstance(criteria, list) else []
        if not criteria:
            warnings.append(f"任务 {fid} 缺验收标准,将按标题验收")

        deps_raw = item.get("depends_on")
        deps = [str(d).strip()[:60] for d in deps_raw[:20] if str(d).strip()] \
            if isinstance(deps_raw, list) else []
        deps = [d for d in deps if d != fid]

        spec = item.get("resource_spec") if isinstance(item.get("resource_spec"), dict) else {}
        if category == "asset":
            outputs, out_warnings = asset_lane.normalize_outputs(spec)
            warnings.extend(f"任务 {fid}:{w}" for w in out_warnings)
            if not outputs:
                warnings.append(f"asset 任务 {fid} 没有任何合法 output,已忽略该任务")
                seen.discard(fid)
                continue
            spec = {
                "skill": str(spec.get("skill") or "image-assets")[:60],
                "style_brief": str(spec.get("style_brief") or "")[:600],
                "outputs": outputs,
                "fallback_allowed": bool(spec.get("fallback_allowed")),
            }
        elif spec:
            spec = {}

        try:
            priority = int(item.get("priority")) if item.get("priority") is not None else None
        except (TypeError, ValueError):
            priority = None
        try:
            max_retries = (
                max(0, min(5, int(item.get("max_retries"))))
                if item.get("max_retries") is not None else None
            )
        except (TypeError, ValueError):
            max_retries = None
        meta = item.get("planner_meta") if isinstance(item.get("planner_meta"), dict) else {}

        tasks.append({
            "feature_id": fid,
            "parent_feature_id": (str(item.get("parent_feature_id") or "").strip()[:60] or None),
            "lane": lane,
            "category": category,
            "title": title,
            "description": (str(item.get("description") or "").strip()[:2000] or None),
            "acceptance_criteria": criteria,
            "depends_on": deps,
            "resource_spec": spec,
            "priority": priority,
            "max_retries": max_retries,
            "planner_meta": meta,
        })

    # Dependency resolution: keep refs to in-plan ids or existing DONE tasks; a ref
    # to an existing pending/blocked task is allowed too (it is on the board), but
    # anything unknown is dropped so it can't dead-block the sprint.
    known = seen | set(existing.keys())
    for t in tasks:
        kept = []
        for d in t["depends_on"]:
            if d in known:
                kept.append(d)
            else:
                warnings.append(f"任务 {t['feature_id']} 依赖未知 feature_id={d},已移除")
        # deps on non-done EXISTING tasks are fine; deps on terminal-not-done get blocked
        # at schedule time by design — surface early instead:
        for d in kept:
            row = existing.get(d)
            if row is not None and d not in done_ids and row.status in (
                DevTaskStatus.TERMINAL - {DevTaskStatus.DONE}
            ):
                warnings.append(f"任务 {t['feature_id']} 依赖 {d} 当前为 {row.status},执行时将被阻塞")
        t["depends_on"] = kept
    warnings.extend(_break_cycles(tasks))

    plan = {
        "version": PLAN_VERSION,
        "summary": str(raw.get("summary") or "")[:1000],
        "assumptions": [str(a)[:300] for a in raw.get("assumptions") or [] if str(a).strip()][:12],
        "target_lanes": sorted({t["lane"] for t in tasks} or {"frontend"}),
        "tasks": tasks,
        "warnings": [str(w)[:300] for w in raw.get("warnings") or [] if str(w).strip()][:12],
    }
    return plan, warnings


def _ledger_requirements(context: dict) -> list[dict]:
    """The ledger's FR/NFR list as ``[{id, statement}]`` (planner input)."""
    try:
        ledger = json.loads(context.get("shared_ledger_raw") or "{}")
    except (json.JSONDecodeError, TypeError):
        ledger = {}
    reqs = ledger.get("requirements") if isinstance(ledger, dict) else []
    return [
        r for r in (reqs or [])
        if isinstance(r, dict) and str(r.get("id") or "").strip()
        and str(r.get("statement") or "").strip()
    ]


def _done_feature_prefixes(context: dict) -> set:
    """FR/NFR ids whose feature is already DONE on the board — these are skipped.

    Only DONE features are 'covered' — NOT merely-seeded pending ones. A fresh dev
    session auto-seeds the board with the ledger's coarse FR/NFR (all pending), so
    skipping every seeded feature would produce an empty plan exactly when it's
    needed most.
    """
    return {
        (b.get("feature_id") or "").split(".")[0]
        for b in context.get("board") or []
        if b.get("feature_id") and b.get("status") == DevTaskStatus.DONE
    }


def _coarse_task_for(rid: str, stmt: str) -> dict:
    """One coarse task for a whole requirement — the per-FR fallback (model split
    unavailable) and the deterministic-fallback unit."""
    return {
        "feature_id": f"{rid}.T1",
        "parent_feature_id": rid,
        "lane": "frontend",
        "category": "nonfunctional" if rid.upper().startswith("NFR") else "functional",
        "title": stmt[:300],
        "acceptance_criteria": [f"{stmt[:400]} — 界面中真实可操作、可验证"],
        "depends_on": [],
        "priority": 0,
    }


def deterministic_fallback(context: dict) -> dict:
    """A conservative plan from the ledger's FR/NFR when the model is unavailable:
    one coarse task per requirement whose feature isn't already DONE."""
    done_features = _done_feature_prefixes(context)
    existing_ids = {
        b.get("feature_id") for b in context.get("board") or [] if b.get("feature_id")
    }
    tasks = []
    for r in _ledger_requirements(context):
        rid, stmt = str(r["id"]).strip(), str(r["statement"]).strip()
        if rid in done_features or f"{rid}.T1" in existing_ids:
            continue
        tasks.append(_coarse_task_for(rid, stmt))
        if len(tasks) >= (context.get("max_tasks") or DEFAULT_MAX_TASKS):
            break
    return {
        "version": PLAN_VERSION,
        "summary": "AI 规划不可用,已按共识账本 FR/NFR 生成保守任务草案。",
        "assumptions": [],
        "target_lanes": ["frontend"],
        "tasks": tasks,
        "warnings": ["degraded:fallback"],
    }


# --- fan-out: per-FR concurrent decomposition on the fast model ------------------
def _fr_split_prompt(rid: str, stmt: str, style_hint: str, max_sub: int) -> str:
    """A SMALL, focused prompt to split ONE requirement into sub-tasks — small
    enough that the fast model answers within budget."""
    return (
        f"你是前端任务规划师。把下面这一个需求({rid})拆成 1~{max_sub} 个可由「单次开发回合」"
        f"完成的小任务,每个任务给 2~4 条可机器/评审验收的标准。任务间若有先后顺序,用 depends_on "
        f"引用本需求内的任务 id。\n\n"
        f"# 需求\n{rid}: {stmt}\n\n"
        f"# 视觉风格(简要参考)\n{style_hint or '(无)'}\n\n"
        f"# 规则\n"
        f"- 只拆这一个需求,不要引入别的需求。\n"
        f"- feature_id 用 {rid}.T1 / {rid}.T2 … 递增。\n"
        f"- 每个任务小到单回合可完成;验收标准具体、可判定,禁止「体验良好」这类空话。\n"
        f"- depends_on 只能引用本需求内已列出的任务 id,不得成环。\n"
        f"# 验收标准硬规则(极重要 —— 评审只看「前端源码 + 运行时界面」,产不出下述东西的标准会永远判不过、导致任务无法闭环)\n"
        f"- 每条验收标准都必须**能从前端源码或运行界面直接判定**(某组件/字段/交互存在且真实可用、点下去有预期结果)。\n"
        f"- **禁止**写需要以下才能验证的标准:编写/提供自动化测试(E2E/单元/集成测试脚本)、检查打包/构建产物(bundle、dist、压缩结果)、"
        f"「遍历所有页面/全站扫描」、后端/网络抓包、以及无可见指标支撑的性能数值(如「首屏<2秒」)。\n"
        f"- 若需求本身是这类非功能/约束(如「禁止收集私钥」「性能」),请把它**改写成可从源码/界面判定的具体检查点**"
        f"(例:表单无 privateKey/mnemonic/password 等敏感字段、未见把用户输入写入 localStorage 或发往第三方、"
        f"关键交互有 loading 态与错误态),而不是要求写测试或查构建产物。\n\n"
        f"# 输出契约(只输出 JSON,不要任何其它文字)\n"
        f'{{"tasks":[{{"feature_id":"{rid}.T1","title":"一句话标题",'
        f'"acceptance_criteria":["标准1","标准2"],"depends_on":[]}}]}}'
    )


def _decompose_one_fr(provider, rid: str, stmt: str, style_hint: str) -> list[dict]:
    """One fast-model call splitting ONE requirement into sub-tasks. Returns the
    task dicts (ids forced into this FR's namespace), or ``[]`` on any failure so
    the caller degrades to the coarse task for this FR."""
    is_nfr = rid.upper().startswith("NFR")
    try:
        r = provider.generate_text(_fr_split_prompt(rid, stmt, style_hint, _FANOUT_MAX_SUB))
    except Exception:  # noqa: BLE001 — a per-FR failure is isolated (coarse fallback)
        return []
    if not getattr(r, "success", False):
        return []
    raw = parse_plan_json(getattr(r, "text", "") or "")
    items = raw.get("tasks") if isinstance(raw, dict) else None
    if not isinstance(items, list) or not items:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for i, it in enumerate(items[:_FANOUT_MAX_SUB]):
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()[:300]
        if not title:
            continue
        fid = str(it.get("feature_id") or "").strip()[:60]
        # Force the id into THIS FR's namespace so a model that echoes a wrong id
        # can't collide across FRs or escape the parent grouping.
        if not fid.startswith(f"{rid}."):
            fid = f"{rid}.T{i + 1}"
        if fid in seen:
            continue
        seen.add(fid)
        crit = it.get("acceptance_criteria")
        crit = [str(c).strip()[:500] for c in crit[:8] if str(c).strip()] \
            if isinstance(crit, list) else []
        deps_raw = it.get("depends_on")
        deps = [
            str(d).strip()[:60] for d in deps_raw[:8]
            if str(d).strip().startswith(f"{rid}.")
        ] if isinstance(deps_raw, list) else []
        out.append({
            "feature_id": fid,
            "parent_feature_id": rid,
            "lane": "frontend",
            "category": "nonfunctional" if is_nfr else "functional",
            "title": title,
            "acceptance_criteria": crit,
            "depends_on": [d for d in deps if d != fid],
            "priority": 0,
        })
    return out


def _fast_split_provider():
    """One shared fast provider (thread-safe client) tuned for per-FR splits: a
    tight per-FR timeout + a single retry (the gateway is occasionally flaky).
    Returns ``None`` when no text provider is configured."""
    from backend.services.ai.factory import get_text_provider

    provider = get_text_provider(force_new=True, role="fast")
    if provider is None or (hasattr(provider, "is_configured") and not provider.is_configured()):
        return None
    try:
        if hasattr(provider, "timeout"):
            provider.timeout = _FANOUT_FR_TIMEOUT
            provider.max_retries = 1
            provider._client = None
            provider._configure()
    except Exception:  # noqa: BLE001
        logger.warning("fanout provider budget override failed", exc_info=True)
    return provider


def fanout_decompose(context: dict, on_progress=None) -> tuple[list[dict], dict]:
    """Concurrently split every not-done ledger FR/NFR into sub-tasks via the fast
    model. Returns ``(tasks, stats)`` where stats = ``{total, ok, fallback,
    truncated}``. A per-FR failure contributes that FR's single coarse task, so the
    result is never empty when there are requirements to plan.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    done_features = _done_feature_prefixes(context)
    reqs = [r for r in _ledger_requirements(context)
            if str(r["id"]).strip() not in done_features]
    if not reqs:
        return [], {"total": 0, "ok": 0, "fallback": 0, "truncated": False}
    truncated = len(reqs) > _FANOUT_MAX_FRS
    reqs = reqs[:_FANOUT_MAX_FRS]
    style_hint = (context.get("style_prompt") or "")[:800]

    # One shared fast provider (thread-safe client) with a tight per-FR budget +
    # a single retry (the gateway is occasionally flaky). ``None`` (no text lane)
    # makes every ``_decompose_one_fr`` degrade to that FR's coarse task.
    provider = _fast_split_provider()

    tasks: list[dict] = []
    ok = fb = 0
    processed = 0

    def _work(r):
        rid, stmt = str(r["id"]).strip(), str(r["statement"]).strip()
        return rid, stmt, _decompose_one_fr(provider, rid, stmt, style_hint)

    with ThreadPoolExecutor(max_workers=min(len(reqs), _FANOUT_MAX_WORKERS)) as ex:
        futures = [ex.submit(_work, r) for r in reqs]
        for fut in as_completed(futures):
            try:
                rid, stmt, sub = fut.result()
            except Exception:  # noqa: BLE001
                continue
            if sub:
                tasks.extend(sub)
                ok += 1
            else:
                tasks.append(_coarse_task_for(rid, stmt))
                fb += 1
            processed += 1
            if on_progress:
                try:
                    on_progress(processed, len(reqs), ok, fb)
                except Exception:  # noqa: BLE001
                    pass
    return tasks, {"total": len(reqs), "ok": ok, "fallback": fb, "truncated": truncated}


def decompose_coarse_seed_tasks(project, session, *, style_hint: str = "",
                                on_progress=None) -> dict:
    """Auto-split a session's PENDING coarse *ledger-seed* tasks into small,
    winnable sub-tasks BEFORE the sprint schedules them.

    A ledger-seed task carries one whole FR as its title with NO granular
    acceptance criteria (``seed_checklist``), so its per-turn verification collapses
    to a single title-level check that bundles every sub-region of the requirement.
    That is structurally unwinnable in one dev turn under the adversarial reviewer
    (FAIL -> repair -> retry -> blocked), which is exactly the "所有验证必然未通过"
    symptom. Here we reuse the per-FR fast-model split (``_decompose_one_fr``) to
    replace each such task with 1~N sub-tasks that each carry concrete AC.

    Contract & safety:
      * candidates = ``source == ledger_seed`` tasks that are PENDING (fresh) or
        BLOCKED (a monolith that already exhausted retries — decomposing it here is
        exactly how a stuck sprint self-heals on its next run), with a TOP-LEVEL
        requirement id (no ``.`` — the dotted ``FR1.T1`` form is already a sub-task
        and must NOT be re-split), no AC yet, and no sub-tasks already on the board
        (``parent_feature_id == feature_id``) — so this is idempotent across re-runs;
        never a task a turn currently owns (ACTIVE states are excluded);
      * children are written FIRST (shared guarded bulk-write, ``source=planner``),
        then the parent is retired to ``skipped`` (a ``SETTLED_OK`` terminal state
        that never blocks sprint completion) — so an FR is never unrepresented;
      * a parent the model can't split (``[]``) is LEFT UNTOUCHED — it runs as
        before (no worse than today), never silently dropped;
      * never raises — model/DB hiccups degrade to "left the board as it was".

    Model calls run concurrently (pure text calls, thread-safe) but every DB write
    happens on THIS thread. Returns ``{candidates, decomposed, sub_tasks, unsplit}``.
    """
    zero = {"candidates": 0, "decomposed": 0, "sub_tasks": 0, "unsplit": 0}
    if not _AUTO_DECOMPOSE_ENABLED:
        return zero
    from concurrent.futures import ThreadPoolExecutor, as_completed

    tasks = dev_sprint_service.session_tasks(session.id)
    has_children = {t.parent_feature_id for t in tasks if t.parent_feature_id}
    candidates = [
        t for t in tasks
        if t.status in (DevTaskStatus.PENDING, DevTaskStatus.BLOCKED)
        and t.source == DevTaskSource.LEDGER_SEED
        and t.feature_id
        and "." not in t.feature_id  # top-level FR/NFR only; dotted ids are sub-tasks
        and t.feature_id not in has_children
        and not t.get_acceptance_criteria()
    ]
    if not candidates:
        return zero
    provider = _fast_split_provider()
    if provider is None:  # no text lane — can't split; run coarse tasks as-is
        return {**zero, "candidates": len(candidates), "unsplit": len(candidates)}
    hint = (style_hint or "")[:800]

    def _work(t):
        fid = str(t.feature_id).strip()
        stmt = (t.description or t.title or "").strip()
        return t.id, fid, _decompose_one_fr(provider, fid, stmt, hint)

    results: list[tuple[str, str, list]] = []
    with ThreadPoolExecutor(max_workers=min(len(candidates), _FANOUT_MAX_WORKERS)) as ex:
        futures = [ex.submit(_work, t) for t in candidates]
        processed = 0
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception:  # noqa: BLE001 — an isolated split failure keeps its parent
                pass
            processed += 1
            if on_progress:
                try:
                    on_progress(processed, len(candidates))
                except Exception:  # noqa: BLE001
                    pass

    decomposed = sub_total = unsplit = 0
    for task_id, _fid, sub in results:
        if not sub:
            unsplit += 1
            continue
        counts = dev_sprint_service.bulk_write_tasks(
            project.id, session.id, sub, source=DevTaskSource.PLANNER,
        )
        child_ids = ", ".join(s["feature_id"] for s in sub)
        # Retire the parent only after its children landed AND only while it is
        # still pending (a racing manual turn may have claimed it — then leave it).
        if dev_sprint_service.retire_superseded(task_id, child_ids):
            decomposed += 1
            sub_total += counts["inserted"] + counts["updated"]
        else:
            unsplit += 1
    return {"candidates": len(candidates), "decomposed": decomposed,
            "sub_tasks": sub_total, "unsplit": unsplit}


def build_raw_plan(context: dict, on_model_call=None, on_progress=None) -> tuple[dict, str]:
    """Produce the raw plan dict + a mode label. Fan-out (per-FR concurrent split on
    the fast model) is the PRIMARY path; a single-shot model call is the fallback for
    projects with no ledger requirements; the deterministic plan is the last resort.

    Modes: ``fanout`` (all FRs model-split) / ``fanout_partial`` (some fell to coarse)
    / ``model`` (single-shot) / ``fallback`` (deterministic).
    """
    if fanout_enabled() and _ledger_requirements(context):
        tasks, stats = fanout_decompose(context, on_progress=on_progress)
        if tasks:
            degraded = stats["ok"] == 0
            mode = "fallback" if degraded else ("fanout_partial" if stats["fallback"] else "fanout")
            summary = (
                "AI 规划不可用,已按共识账本 FR/NFR 生成保守任务草案。" if degraded
                else f"已将 {stats['total']} 个需求细拆为 {len(tasks)} 个可单回合完成的任务"
                     + (f"(其中 {stats['fallback']} 个需求拆分失败,退回粗任务)" if stats["fallback"] else "")
                     + ("(需求过多,已截断)" if stats.get("truncated") else "") + "。"
            )
            plan = {
                "version": PLAN_VERSION,
                "summary": summary,
                "assumptions": [],
                "target_lanes": sorted({t.get("lane", "frontend") for t in tasks}),
                "tasks": tasks,
                "warnings": ["degraded:fallback"] if degraded else [],
            }
            return plan, mode

    # No ledger requirements (or fan-out disabled) → single-shot model, then fallback.
    prompt = None
    try:
        prompt = render_planner_prompt(context)
    except Exception:  # noqa: BLE001
        logger.warning("planner prompt render failed", exc_info=True)
    if prompt:
        result = call_planner_model(prompt, on_model_call=on_model_call)
        if result is not None and getattr(result, "success", False):
            raw = parse_plan_json(result.text)
            if raw:
                return raw, "model"
    return deterministic_fallback(context), "fallback"


# --- apply ---------------------------------------------------------------------------
class PlanStale(ValueError):
    """The plan's input fingerprint no longer matches the live project state."""


class PlanNotApplicable(ValueError):
    """The plan is not in an applicable status."""


def apply_plan(plan_row: CodeDevTaskPlan, project, session, *, replace: bool = False,
               force: bool = False) -> dict:
    """Fold a draft plan onto the task board through the shared bulk-write path.

    Fingerprint-guarded: when the docs/ledger/board drifted since generation the
    plan flips to ``stale`` and apply refuses unless ``force=True``. Counts are
    recorded on the plan row; status ends ``applied`` (or ``failed`` on error).
    """
    if plan_row.status not in (DevTaskPlanStatus.DRAFT, DevTaskPlanStatus.STALE):
        raise PlanNotApplicable(f"计划当前状态({plan_row.status})不可应用")
    plan = plan_row.get_plan()
    request = plan.get("request") or {}
    context = build_planner_context(
        project, session,
        target_lanes=plan_row.get_target_lanes() or None,
        include_assets=bool(request.get("include_assets", True)),
        max_tasks=int(request.get("max_tasks") or DEFAULT_MAX_TASKS),
        instruction=str(request.get("instruction") or ""),
    )
    if plan_row.input_fingerprint and input_fingerprint(context) != plan_row.input_fingerprint:
        if not force:
            plan_row.status = DevTaskPlanStatus.STALE
            db.session.commit()
            raise PlanStale("项目文档/任务板已变化,计划已过期;请重新生成或 force 应用")

    plan_row.status = DevTaskPlanStatus.APPLYING
    db.session.commit()
    try:
        counts = dev_sprint_service.bulk_write_tasks(
            plan_row.project_id, plan_row.session_id, plan.get("tasks") or [],
            replace=replace, plan_id=plan_row.id, source=DevTaskSource.PLANNER,
        )
        plan_row.status = DevTaskPlanStatus.APPLIED
        plan_row.inserted_count = counts["inserted"]
        plan_row.updated_count = counts["updated"]
        plan_row.skipped_count = counts["skipped"]
        plan_row.applied_at = datetime.utcnow()
        db.session.commit()
        return counts
    except dev_sprint_service.BulkWriteRefused:
        plan_row.status = DevTaskPlanStatus.DRAFT  # refused, still applicable later
        db.session.commit()
        raise
    except Exception as exc:  # noqa: BLE001 — record then re-raise
        db.session.rollback()
        plan_row.status = DevTaskPlanStatus.FAILED
        plan_row.error_message = str(exc)[:1000]
        db.session.commit()
        raise


def check_staleness(plan_row: CodeDevTaskPlan, project, session) -> bool:
    """Lazily flip a DRAFT plan to STALE when its inputs drifted. Returns staleness."""
    if plan_row.status != DevTaskPlanStatus.DRAFT or not plan_row.input_fingerprint:
        return plan_row.status == DevTaskPlanStatus.STALE
    plan = plan_row.get_plan()
    request = plan.get("request") or {}
    context = build_planner_context(
        project, session,
        target_lanes=plan_row.get_target_lanes() or None,
        include_assets=bool(request.get("include_assets", True)),
        max_tasks=int(request.get("max_tasks") or DEFAULT_MAX_TASKS),
        instruction=str(request.get("instruction") or ""),
    )
    if input_fingerprint(context) != plan_row.input_fingerprint:
        plan_row.status = DevTaskPlanStatus.STALE
        db.session.commit()
        return True
    return False
