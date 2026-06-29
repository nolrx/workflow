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

from dataclasses import dataclass, field

from backend.services.code import house_rules

_MAX_FEATURES = 60
_MAX_RUNTIME_ERRORS = 30


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

    @property
    def review_blocking(self) -> list[str]:
        r = self.review or {}
        return [str(x)[:300] for x in (r.get("blocking_issues") or [])]

    @property
    def blocking(self) -> bool:
        """Block only on OBJECTIVE defects (deterministic or explicitly flagged)."""
        return bool(self.house_rule_errors or self.runtime_errors or self.review_blocking)

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
        "feature_results": feature_results,
        "fr_coverage": revs[0].get("fr_coverage"),
        "blocking_issues": blocking,
        "advisory_issues": advisory[:30],
        "issues": [i for r in revs for i in (r.get("issues") or [])][:30],
        "summary": (f"[{n}-评审共识:{flagging}/{n} 判阻断] " + (summaries[0] if summaries else "")).strip(),
        "panel": {"n": n, "flagging_blocking": flagging, "panel_blocking": panel_blocking},
    }
