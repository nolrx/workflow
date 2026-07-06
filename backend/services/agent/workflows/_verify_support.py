"""
Shared verify->repair helpers for the project-generation workflows
(``code_frontend_project_generation`` / ``code_backend_project_generation``).

Encodes the article-derived quality loop in ONE place so frontend and backend
share it:

  * **features checklist (P1-D)** — derive a machine-checkable acceptance list
    from the consensus ledger's FR/NFR registry, every item starting
    ``passes=false`` (JSON-shaped, never deleted), then fold the evaluator's
    per-item verdicts back on.
  * **verification verdict** — combine the deterministic house-rules linter
    findings + the runtime browser-smoke findings + the skeptical rubric
    evaluator's explicit ``blocking_issues`` into ONE blocking decision that
    drives the bounded repair loop.

Gate philosophy (the OpenAI/Anthropic synthesis): block only on OBJECTIVE,
mechanically-or-explicitly-flagged defects (house-rule errors, runtime errors,
the evaluator's blocking_issues). A subjective FAIL verdict or an unimplemented
feature is RECORDED and added to the repair brief when a repair is already
triggered, but never by itself causes churn.

The repair *mechanism* (which container call) differs per stack, so the loop body
lives in each workflow; this module only provides the stack-agnostic pieces. Pure
and import-light so it is unit-testable without Docker.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from backend.services.code import house_rules

_MAX_FEATURES = 60
_MAX_RUNTIME_ERRORS = 30

# Canonical rubric weights — MUST match the weights baked into the critic prompts
# (code/{frontend,backend}_project_critic_prompt.txt: design×.35 + originality×.25
# + craft×.20 + functionality×.20). Kept here as the single programmatic source so
# the trend reporting / eval / (future) score gate can recompute a weighted_score
# when the model — or a panel aggregate — didn't emit one.
RUBRIC_WEIGHTS = {
    "design_quality": 0.35,
    "originality": 0.25,
    "craft": 0.20,
    "functionality": 0.20,
}


def weighted_score_of(review: dict | None) -> float | None:
    """The review's overall rubric score (0~5), or ``None`` if not derivable.

    Prefers the model's own ``weighted_score``; falls back to recomputing it from
    the per-dimension ``scores`` with :data:`RUBRIC_WEIGHTS` (normalised over the
    dimensions actually present) so a panel-aggregated review — which only averages
    the per-dimension scores and does NOT re-emit ``weighted_score`` — still yields
    one gradable number.
    """
    if not isinstance(review, dict):
        return None
    ws = review.get("weighted_score")
    if isinstance(ws, (int, float)):
        return round(float(ws), 2)
    scores = review.get("scores")
    if not isinstance(scores, dict):
        return None
    total = wsum = 0.0
    for key, weight in RUBRIC_WEIGHTS.items():
        val = scores.get(key)
        if isinstance(val, (int, float)):
            total += float(val) * weight
            wsum += weight
    return round(total / wsum, 2) if wsum > 0 else None


# --- features checklist (P1-D) ----------------------------------------------
def features_from_ledger(ledger_dict: dict | None) -> list[dict]:
    """Derive an acceptance checklist from the ledger's FR/NFR registry.

    Every feature starts ``passes=false`` (Anthropic's "all failing initially").
    JSON-shaped so the model is less likely to silently rewrite it.
    """
    reqs = (ledger_dict or {}).get("requirements") or []
    feats: list[dict] = []
    for r in reqs:
        if not isinstance(r, dict):
            continue
        rid = r.get("id")
        stmt = r.get("statement")
        if not rid or not stmt:
            continue
        category = "non_functional" if str(rid).upper().startswith("NFR") else "functional"
        feats.append({
            "id": str(rid),
            "category": category,
            "description": str(stmt)[:400],
            "passes": False,
            "note": "",
        })
        if len(feats) >= _MAX_FEATURES:
            break
    return feats


def render_features_block(features: list[dict]) -> str:
    """Compact list for the evaluator prompt (it must judge each item pass/fail)."""
    if not features:
        return ""
    lines = ["# 验收功能清单(逐条判定是否真正实现:passes=true/false,note 给证据或缺失)"]
    for f in features:
        lines.append(f"- [{f['id']}] ({f.get('category', 'functional')}) {f['description']}")
    return "\n".join(lines)


def apply_feature_results(features: list[dict], results) -> tuple[list[dict], dict]:
    """Fold the evaluator's ``feature_results`` ([{id,passes,note}]) onto the list."""
    by_id: dict[str, dict] = {}
    for item in (results or []):
        if isinstance(item, dict) and item.get("id"):
            by_id[str(item["id"])] = item
    updated: list[dict] = []
    passed = 0
    for f in features:
        nf = dict(f)
        r = by_id.get(str(f["id"]))
        if r is not None:
            nf["passes"] = bool(r.get("passes"))
            nf["note"] = str(r.get("note") or "")[:400]
        updated.append(nf)
        if nf["passes"]:
            passed += 1
    stats = {"total": len(updated), "passed": passed, "failed": len(updated) - passed}
    return updated, stats


def failed_functional_features(features: list[dict]) -> list[dict]:
    return [f for f in features if f.get("category") == "functional" and not f.get("passes")]


# --- runtime browser-smoke findings (P1-C) ----------------------------------
def runtime_errors(runtime_check: dict | None) -> list[str]:
    """Console + page errors captured by the in-container interactive smoke.

    Empty when the smoke didn't run (no browser in the image — fail-soft) or the
    page loaded + was driven clean, so it only ever gates on a REAL runtime error
    (incl. errors thrown WHILE clicking controls / submitting forms).
    """
    rc = runtime_check or {}
    if not rc.get("ran"):
        return []
    errs = list(rc.get("console_errors") or []) + list(rc.get("page_errors") or [])
    return [str(e)[:300] for e in errs][:_MAX_RUNTIME_ERRORS]


def dead_controls(runtime_check: dict | None) -> list[str]:
    """Controls the interactive crawl clicked that changed nothing (suspect dead).

    A SOFT signal (never blocks by itself) — fed to the evaluator + repair brief so
    a likely-non-functional button/link gets verified/fixed, but a legitimate no-op
    (e.g. an already-active tab) doesn't hard-fail the run.
    """
    inter = (runtime_check or {}).get("interactions") or {}
    return [str(d)[:60] for d in (inter.get("dead_controls") or [])][:20]


def render_runtime_report(runtime_check: dict | None) -> str:
    """Full interactive-smoke report for the evaluator + repair brief: hard errors,
    interaction coverage, and suspect dead controls."""
    rc = runtime_check or {}
    if not rc.get("ran"):
        return ""
    errs = runtime_errors(rc)
    inter = rc.get("interactions") or {}
    dead = dead_controls(rc)
    lines: list[str] = []
    if errs:
        lines.append("# 运行时冒烟发现以下错误(浏览器加载/交互构建产物时实际抛出,必须修复):")
        lines.extend(f"- {e}" for e in errs)
    cov = (
        f"交互覆盖:点击 {inter.get('clicked', 0)}/{inter.get('total', 0)} 个控件,"
        f"填写 {inter.get('filled', 0)} 个输入。"
    )
    if dead:
        lines.append("# 疑似无效控件(点击后 DOM/路由/网络/状态均无变化 — 请核实是否漏接事件,逐项确认或修复):")
        lines.extend(f"- {d}" for d in dead)
    if not errs and not dead:
        lines.append(f"运行时冒烟:已加载并交互,未捕获 console/page error。{cov}")
    elif not errs:
        lines.append(cov)
    return "\n".join(lines)


# --- combined verdict -------------------------------------------------------
@dataclass
class Verification:
    """One round's combined verdict over a generated project."""

    house_rule_errors: list = field(default_factory=list)     # [Violation]
    house_rule_warnings: list = field(default_factory=list)   # [Violation]
    runtime_errors: list[str] = field(default_factory=list)
    runtime_check: dict | None = None                          # full interactive-smoke result
    review: dict | None = None
    features: list[dict] = field(default_factory=list)
    feature_stats: dict = field(default_factory=dict)
    # A1 score gate (env CODE_QUALITY_MIN_SCORE / per-dim). None = OFF → behaviour
    # unchanged. Only ever bites when the judge actually produced a score.
    min_weighted_score: float | None = None
    min_dim_scores: dict | None = None

    @property
    def review_blocking(self) -> list[str]:
        r = self.review or {}
        return [str(x)[:300] for x in (r.get("blocking_issues") or [])]

    @property
    def weighted_score(self) -> float | None:
        return weighted_score_of(self.review)

    @property
    def score_blocking(self) -> list[str]:
        """A1: rubric score below the configured floor(s).

        Fires only when (a) a floor is configured AND (b) the judge produced a
        score — a build the judge couldn't evaluate (review None / no score) is
        never penalised by the score gate (the objective blockers still apply).
        """
        if self.review is None:
            return []
        reasons: list[str] = []
        if self.min_weighted_score is not None:
            ws = self.weighted_score
            if ws is not None and ws < self.min_weighted_score:
                reasons.append(f"质量总分 {ws} 低于阈值 {self.min_weighted_score}")
        if self.min_dim_scores:
            scores = self.review.get("scores") if isinstance(self.review.get("scores"), dict) else {}
            for dim, floor in self.min_dim_scores.items():
                val = scores.get(dim)
                if floor is not None and isinstance(val, (int, float)) and val < floor:
                    reasons.append(f"{dim} 维度分 {val} 低于阈值 {floor}")
        return reasons

    @property
    def blocking(self) -> bool:
        """Block on OBJECTIVE defects (deterministic or explicitly flagged) or, when a
        score floor is configured (A1), a sub-threshold rubric score."""
        return bool(self.house_rule_errors or self.runtime_errors
                    or self.review_blocking or self.score_blocking)

    @property
    def objective_blocking(self) -> bool:
        """Hard, deterministic blockers ONLY: house-rule errors, runtime errors, and
        the configured score gate. EXCLUDES the reviewer's subjective, free-form
        ``blocking_issues`` (``review_blocking``).

        Used to gate a single dev-task turn's closure/repair: those blocking_issues
        are judged over the WHOLE app against a truncated source digest, so a page
        the reviewer couldn't see ("源码未提供") would otherwise fail an unrelated
        task forever. A task is judged on its own AC (feature_results) + these hard
        signals; the reviewer's whole-app verdict stays advisory."""
        return bool(self.house_rule_errors or self.runtime_errors or self.score_blocking)

    def repair_instruction(self) -> str:
        """The change brief fed to the edit-mode rebuild (one repair round).

        Leads with a HANDOFF (Article 1 reset+handoff / Article 2 progress file):
        the repair runs as a fresh agent seeded with the current source, so it is
        told which features already pass (don't touch / rewrite) and to only fix the
        listed problems — incremental repair, not regeneration.
        """
        parts: list[str] = []
        passed = [f for f in self.features if f.get("category") == "functional" and f.get("passes")]
        if passed:
            parts.append(
                "# 续修交接(增量修复:只修下述问题,已通过的功能请勿改动 / 重写)\n"
                "## 已通过(保持不变):" + "、".join(f"[{f['id']}]" for f in passed[:30])
            )
        hr = list(self.house_rule_errors) + list(self.house_rule_warnings)
        if hr:
            parts.append(house_rules.render_report(hr))
        rt = (
            render_runtime_report(self.runtime_check) if self.runtime_check
            else render_runtime_report({"ran": True, "console_errors": self.runtime_errors})
            if self.runtime_errors else ""
        )
        if rt:
            parts.append(rt)
        if self.review_blocking:
            parts.append(
                "# 验收评审判定的必须修复项:\n"
                + "\n".join(f"- {x}" for x in self.review_blocking)
            )
        failed = failed_functional_features(self.features)
        if failed:
            parts.append(
                "# 验收清单中尚未实现的功能(请补全为真实可用实现,禁止占位/TODO):\n"
                + "\n".join(f"- [{f['id']}] {f['description']}" for f in failed)
            )
        # A1: when the score gate fires, fold the evaluator's advisory (polish) items
        # back in as quality-improvement targets — the design/originality/craft work
        # that is normally demoted to advisory now becomes load-bearing for the score.
        sb = self.score_blocking
        if sb:
            advisory = [str(x)[:200] for x in ((self.review or {}).get("advisory_issues") or [])][:8]
            lines = [
                "# 质量未达阈值(在不改动已通过功能、不破坏现有实现与构建的前提下,"
                "提升设计质量 / 原创性 / 工艺):"
            ]
            lines += [f"- {s}" for s in sb]
            if advisory:
                lines.append("可优先处理以下打磨项:")
                lines += [f"- {a}" for a in advisory]
            parts.append("\n".join(lines))
        return "\n\n".join(p for p in parts if p)

    def summary_line(self) -> str:
        bits = []
        if self.house_rule_errors:
            bits.append(f"房规违规 {len(self.house_rule_errors)}")
        if self.house_rule_warnings:
            bits.append(f"房规建议 {len(self.house_rule_warnings)}")
        if self.runtime_errors:
            bits.append(f"运行时错误 {len(self.runtime_errors)}")
        _dc = dead_controls(self.runtime_check)
        if _dc:
            bits.append(f"疑似无效控件 {len(_dc)}")
        if self.review:
            bits.append(f"评审 {str(self.review.get('verdict') or '—')}")
        if self.feature_stats:
            bits.append(f"功能 {self.feature_stats.get('passed', 0)}/{self.feature_stats.get('total', 0)}")
        return "；".join(bits) or "无明显问题"

    def to_record(self) -> dict:
        hr = list(self.house_rule_errors) + list(self.house_rule_warnings)
        return {
            "blocking": self.blocking,
            "house_rules": house_rules.to_dicts(hr),
            "house_rule_summary": house_rules.summarize(hr),
            "runtime_errors": self.runtime_errors,
            "interactions": (self.runtime_check or {}).get("interactions"),
            "dead_controls": dead_controls(self.runtime_check),
            "review": self.review,
            "features": self.features,
            "feature_stats": self.feature_stats,
            "weighted_score": self.weighted_score,
            "score_blocking": self.score_blocking,
        }


# --- reviewer panel / consensus (②a) ----------------------------------------
# Distinct审查视角 so an N-reviewer panel sees the build through different lenses
# (diversity catches failure modes a single framing misses), then majority-votes.
REVIEW_LENSES_FRONTEND = [
    "正确性:每个核心动作(按钮/表单/导航)点下去是否真的产生预期结果,有无死控件 / 空事件处理。",
    "用户任务:以终端用户视角,核心场景能否端到端走通完成(而非只是页面好看)。",
    "契约一致:全栈模式下是否真的调 window.__API_BASE__ 的真实后端、按统一信封拆 resp.data、登录走 data.token。",
]
REVIEW_LENSES_BACKEND = [
    "契约一致:逐端点的路径 / 方法 / 字段是否与契约逐字一致、统一信封、登录 token 落在 data.token。",
    "安全:鉴权是否正确、有无越权 / 缺失校验 / 跨租户串读 / 敏感信息泄露。",
    "健壮与可部署:错误处理 / 边界 / 校验是否完备,Dockerfile / /health / env 读取 / 空库自播种是否到位。",
]


# --- A1/A3 gate configuration (all env-gated; defaults preserve behaviour) ---
_DIM_ENVS = {
    "design_quality": "CODE_QUALITY_MIN_DESIGN",
    "originality": "CODE_QUALITY_MIN_ORIGINALITY",
    "craft": "CODE_QUALITY_MIN_CRAFT",
    "functionality": "CODE_QUALITY_MIN_FUNCTIONALITY",
}


def env_score_floor() -> float | None:
    """``CODE_QUALITY_MIN_SCORE`` as a float, or None when unset/blank/invalid.

    None = score gate OFF → ``Verification.blocking`` is unchanged from pre-A1.
    """
    raw = os.getenv("CODE_QUALITY_MIN_SCORE")
    if not raw or not raw.strip():
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def env_dim_floors() -> dict | None:
    """Per-dimension score floors from env (design/originality/craft/functionality),
    or None when none are set."""
    out: dict[str, float] = {}
    for dim, env in _DIM_ENVS.items():
        raw = os.getenv(env)
        if raw and raw.strip():
            try:
                out[dim] = float(raw)
            except ValueError:
                pass
    return out or None


def pivot_enabled() -> bool:
    """A3 refine-vs-pivot toggle (env CODE_REPAIR_PIVOT). Default OFF."""
    return os.getenv("CODE_REPAIR_PIVOT", "0").strip().lower() not in ("0", "", "false", "no")


# --- P-A: acceptance-driven iteration (all env-gated; defaults preserve behaviour) ---
def iterate_to_acceptance() -> bool:
    """``CODE_ITERATE_TO_ACCEPTANCE`` — when ON, the repair loop keeps going (within
    budget) while functional features remain unmet, not only while ``blocking`` is
    true. Default OFF → loop stops as soon as nothing is blocking (pre-P-A behaviour)."""
    return os.getenv("CODE_ITERATE_TO_ACCEPTANCE", "0").strip().lower() not in ("0", "", "false", "no")


def iterate_max_rounds(verify_default: int) -> int:
    """The acceptance loop's repair-round budget. ``CODE_ITERATE_MAX_ROUNDS`` when set
    (and >= 0), otherwise falls back to ``verify_default`` (the caller's
    ``CODE_VERIFY_MAX_ROUNDS`` value) so an unset env changes nothing."""
    raw = os.getenv("CODE_ITERATE_MAX_ROUNDS")
    if raw and raw.strip():
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return verify_default


def iterate_stall() -> int:
    """Consecutive no-coverage-gain rounds that trigger an early stop
    (``CODE_ITERATE_STALL``, default 2). ``<=0`` disables the stall guard."""
    raw = os.getenv("CODE_ITERATE_STALL", "2")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 2


def functional_coverage(features: list[dict]) -> tuple[int, int]:
    """(passed, total) over FUNCTIONAL features only — the acceptance metric."""
    funcs = [f for f in (features or []) if f.get("category") == "functional"]
    return sum(1 for f in funcs if f.get("passes")), len(funcs)


def _stalled(coverage_history: list | None, stall: int) -> bool:
    """True when functional pass-count hasn't increased across the last ``stall`` rounds.
    ``coverage_history`` is the passed-count after each completed round (oldest first).
    Needs ``stall+1`` points; comparing current vs ``stall`` rounds back."""
    nums = [c for c in (coverage_history or []) if isinstance(c, (int, float))]
    if stall <= 0 or len(nums) < stall + 1:
        return False
    window = nums[-(stall + 1):]
    return window[-1] <= window[0]


def should_stop(
    verification: "Verification",
    round_idx: int,
    max_rounds: int,
    *,
    to_acceptance: bool = False,
    coverage_history: list | None = None,
    stall: int = 2,
) -> tuple[bool, str]:
    """Unified repair-loop termination predicate, shared by FE/BE workflows.

    ``round_idx`` = repair rounds already done (the loop is deciding whether to do
    another). Returns ``(stop, reason)``.

    With ``to_acceptance=False`` (default) this is **byte-identical** to the legacy
    ``not verification.blocking or round_idx >= max_rounds`` break condition.

    With ``to_acceptance=True`` the loop also continues while functional features are
    unmet, and stops on three conditions: reached budget / fully accepted (nothing
    blocking AND no unmet functional feature) / stalled (no coverage gain for
    ``stall`` rounds).
    """
    if round_idx >= max_rounds:
        return True, "已达迭代轮数上限"
    if not to_acceptance:
        return (not verification.blocking), ""
    if not verification.blocking and not failed_functional_features(verification.features):
        return True, "已达标:无阻断且功能清单全部通过"
    if _stalled(coverage_history, stall):
        return True, f"连续 {stall} 轮功能覆盖无增长,提前停止"
    return False, ""


# --- P-B: incremental batched build (env-gated; default 1 = single build) ----
def build_batches() -> int:
    """``CODE_BUILD_BATCHES`` — split generation into N incremental waves so a large
    app (game / mid-back-office) is built feature-by-feature instead of one giant
    pass. Default 1 = single monolithic build (pre-P-B behaviour)."""
    raw = os.getenv("CODE_BUILD_BATCHES", "1")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


def split_batches(features: list[dict], n: int) -> list[list[dict]]:
    """Split FUNCTIONAL features into ``n`` contiguous near-equal batches.

    Returns ``[]`` when batching does not apply (n<=1, or <2 functional features, or
    fewer functional features than batches) so the caller falls back to one build.
    Contiguous chunks preserve the requirements' authored order (related features
    tend to be listed together); smarter semantic grouping is a future enhancement.
    """
    funcs = [f for f in (features or []) if f.get("category") == "functional"]
    if n <= 1 or len(funcs) < 2 or len(funcs) < n:
        return []
    size = (len(funcs) + n - 1) // n  # ceil → at most n chunks
    return [funcs[i:i + size] for i in range(0, len(funcs), size)]


def render_feature_subset(batch: list[dict], idx: int, total: int) -> str:
    """Change brief for incremental build wave ``idx`` of ``total`` — fed through the
    existing edit-mode path (base_files + change_instruction), so NO prompt change."""
    lines = [
        f"# 增量构建 第 {idx + 1}/{total} 批:在现有工程基础上完整实现以下功能"
        "(端到端真实可用,禁止占位/TODO),并保持已实现功能与构建不被破坏:"
    ]
    lines += [f"- [{f['id']}] {f.get('description', '')}" for f in batch]
    return "\n".join(lines)


# When a targeted repair round doesn't move the rubric score, escalate the brief from
# "only fix the listed defects" to "you may refactor this module" — Article 1's
# score-trend-driven refine-vs-pivot. Opt-in (pivot_enabled) so default repair is
# unchanged; naturally dormant until the judge produces a score trend.
REPAIR_PIVOT_PLAN = (
    "上一轮定向修复后质量/功能仍未达标且无明显改善:可对相关模块做较大幅度的重构"
    "(允许重写该模块),但必须保持已通过的功能可用、不破坏构建,不要只做表面微调。"
)


def should_pivot(score_history: list, min_gain: float = 0.2) -> bool:
    """True when the rubric score has STALLED across rounds (refine isn't helping).

    Needs >=2 scored rounds; pivots when the latest round gained < ``min_gain`` over
    the previous. With <2 scores (judge down / first round) returns False, so the loop
    keeps doing ordinary targeted repair.
    """
    nums = [s for s in (score_history or []) if isinstance(s, (int, float))]
    if len(nums) < 2:
        return False
    return (nums[-1] - nums[-2]) < min_gain


def repair_regressed(prior: "Verification", cand: "Verification") -> tuple[bool, str]:
    """True when a repair round made the build WORSE on a HARD signal — the generation
    analog of the deploy repair regression guard.

    Hard regressions (revert to the prior, better artifact instead of adopting the
    repair): a functional feature that PASSED before now fails, or the repair
    introduced new house-rule errors / new runtime errors. Soft metric wobble (a
    lower polish score) is NOT a regression — a repair is allowed to trade polish for
    fixing a blocker. Panel consensus (CODE_REVIEW_PANEL>1) dampens the per-feature
    judgment noise, so a pass->fail flip is a meaningful signal, not a coin toss.
    """
    prior_pass = {
        str(f.get("id")) for f in (prior.features or [])
        if f.get("category") == "functional" and f.get("passes")
    }
    cand_fail = {
        str(f.get("id")) for f in (cand.features or [])
        if f.get("category") == "functional" and not f.get("passes")
    }
    broke = sorted(prior_pass & cand_fail)
    if broke:
        return True, f"原本通过的功能回退:{', '.join(broke[:5])}"
    if len(cand.house_rule_errors) > len(prior.house_rule_errors):
        return True, f"房规错误增多({len(prior.house_rule_errors)}→{len(cand.house_rule_errors)})"
    if len(cand.runtime_errors) > len(prior.runtime_errors):
        return True, f"运行时错误增多({len(prior.runtime_errors)}→{len(cand.runtime_errors)})"
    return False, ""


def _safe_call(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001 — one reviewer failing must not sink the panel
        return None


def run_reviewers(thunks: list) -> list:
    """Run independent reviewer thunks (each ONE I/O-bound text-model call) CONCURRENTLY.

    A consensus panel's reviews are independent and ``review_project`` does no DB work,
    so N reviewers finish in ~one call's wall-clock instead of N sequential calls.
    **Charging (DB) must happen on the caller's thread BEFORE this** — only the model
    calls run here, so there is no cross-thread session sharing. A thunk that throws
    yields ``None`` (filter before aggregating).
    """
    if not thunks:
        return []
    if len(thunks) == 1:
        return [_safe_call(thunks[0])]
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(len(thunks), 8)) as pool:
        return list(pool.map(_safe_call, thunks))


def _dedup(items: list) -> list:
    seen, out = set(), []
    for x in items:
        s = str(x)
        if s not in seen:
            seen.add(s)
            out.append(x)
    return out


def aggregate_reviews(reviews) -> dict | None:
    """Majority consensus over N independent reviewer outputs (adversarial panel).

    Single review (or N=1) is returned unchanged. With N>1: a build is blocking
    only when **≥ half** the reviewers flag it (verdict FAIL or non-empty
    blocking_issues) — so one harsh reviewer can't trigger needless repair churn,
    while a minority's concerns are demoted to advisory. Per-feature pass requires
    a strict majority; scores are averaged.
    """
    revs = [r for r in (reviews or []) if isinstance(r, dict)]
    if not revs:
        return None
    if len(revs) == 1:
        return revs[0]
    n = len(revs)
    flagging = sum(
        1 for r in revs
        if str(r.get("verdict", "")).upper() == "FAIL" or (r.get("blocking_issues") or [])
    )
    panel_blocking = flagging * 2 >= n  # ≥ half flag → block (skeptical-leaning)

    by_id: dict[str, list] = {}
    for r in revs:
        for fr in (r.get("feature_results") or []):
            if isinstance(fr, dict) and fr.get("id"):
                by_id.setdefault(str(fr["id"]), []).append(fr)
    feature_results = []
    for fid, frs in by_id.items():
        pass_votes = sum(1 for f in frs if f.get("passes"))
        passes = pass_votes * 2 > len(frs)  # strict majority to count as passing
        note = next((f.get("note") for f in frs if not f.get("passes") and f.get("note")),
                    (frs[0].get("note") if frs else ""))
        feature_results.append({"id": fid, "passes": passes, "note": (note or "")[:400]})

    all_blocking = _dedup([x for r in revs for x in (r.get("blocking_issues") or [])])
    all_advisory = _dedup([x for r in revs for x in (r.get("advisory_issues") or [])])
    verdicts = [str(r.get("verdict", "")).upper() for r in revs]
    if panel_blocking:
        verdict, blocking, advisory = "FAIL", all_blocking, all_advisory
    else:
        verdict = "CONCERNS" if any(v in ("CONCERNS", "FAIL") for v in verdicts) else "PASS"
        blocking, advisory = [], _dedup(all_advisory + all_blocking)  # minority blocking → advisory

    score_keys = {k for r in revs if isinstance(r.get("scores"), dict) for k in r["scores"]}
    scores = {}
    for k in score_keys:
        vals = [r["scores"][k] for r in revs
                if isinstance(r.get("scores"), dict) and isinstance(r["scores"].get(k), (int, float))]
        if vals:
            scores[k] = round(sum(vals) / len(vals), 1)
    summaries = [r.get("summary") for r in revs if r.get("summary")]
    return {
        "verdict": verdict,
        "scores": scores or None,
        # The aggregate only averages per-dimension scores, so re-derive the single
        # weighted_score (the model emits it per-review but not for the consensus).
        "weighted_score": weighted_score_of({"scores": scores}) if scores else None,
        "feature_results": feature_results,
        "fr_coverage": revs[0].get("fr_coverage"),
        "blocking_issues": blocking,
        "advisory_issues": advisory[:30],
        "issues": [i for r in revs for i in (r.get("issues") or [])][:30],
        "summary": (f"[{n}-评审共识:{flagging}/{n} 判阻断] " + (summaries[0] if summaries else "")).strip(),
        "panel": {"n": n, "flagging_blocking": flagging, "panel_blocking": panel_blocking},
    }
