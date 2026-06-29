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
    """Console + page errors captured by the in-container headless browser.

    Empty when the smoke didn't run (no browser in the image — fail-soft) or the
    page loaded clean, so it only ever gates when there is a REAL runtime error.
    """
    rc = runtime_check or {}
    if not rc.get("ran"):
        return []
    errs = list(rc.get("console_errors") or []) + list(rc.get("page_errors") or [])
    return [str(e)[:300] for e in errs][:_MAX_RUNTIME_ERRORS]


def render_runtime_report(errors: list[str]) -> str:
    if not errors:
        return ""
    lines = ["# 运行时冒烟发现以下错误(浏览器加载构建产物时实际抛出,必须修复):"]
    lines.extend(f"- {e}" for e in errors)
    return "\n".join(lines)


# --- combined verdict -------------------------------------------------------
@dataclass
class Verification:
    """One round's combined verdict over a generated project."""

    house_rule_errors: list = field(default_factory=list)     # [Violation]
    house_rule_warnings: list = field(default_factory=list)   # [Violation]
    runtime_errors: list[str] = field(default_factory=list)
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
        """The change brief fed to the edit-mode rebuild (one repair round)."""
        parts: list[str] = []
        hr = list(self.house_rule_errors) + list(self.house_rule_warnings)
        if hr:
            parts.append(house_rules.render_report(hr))
        if self.runtime_errors:
            parts.append(render_runtime_report(self.runtime_errors))
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
            "review": self.review,
            "features": self.features,
            "feature_stats": self.feature_stats,
        }
