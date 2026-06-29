#!/usr/bin/env python3
"""
Offline eval for the skeptical project evaluator (P0-B / ②b).

You can't improve a judge you don't measure. This runs the REAL evaluator
(``FrontendProjectService.review_project``, on the text lane) over a small set of
LABELED fixtures — clean apps and deliberately-broken ones — and reports whether
the evaluator DISCRIMINATES: does it block the broken builds (verdict FAIL or
non-empty ``blocking_issues``) without fabricating defects on the clean one?

Calls a live model, so it needs ``AI_TEXT_*`` configured (and uses the Mongo /
bundled critic prompt). It is also wired as ``tests/test_review_eval.py`` (marked
``integration``) so it doubles as a regression guard for the critic prompt.

    uv run python scripts/eval_review.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:  # standalone runs need .env so the text-lane provider resolves its key
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

from backend.services.code import house_rules  # noqa: E402

_GOOD_TODO = """// ===== src/App.tsx =====
import { useState } from 'react'
export default function App() {
  const [todos, setTodos] = useState<string[]>([])
  const [t, setT] = useState('')
  return (
    <div>
      <input value={t} onChange={(e) => setT(e.target.value)} placeholder="待办" />
      <button onClick={() => { if (t) { setTodos([...todos, t]); setT('') } }}>添加</button>
      <ul>{todos.map((x, i) => (
        <li key={i}>{x} <button onClick={() => setTodos(todos.filter((_, j) => j !== i))}>删除</button></li>
      ))}</ul>
    </div>
  )
}
"""

_BAD_DEAD = """// ===== src/App.tsx =====
import { useState } from 'react'
export default function App() {
  const [todos] = useState<string[]>(['示例待办'])
  const [t, setT] = useState('')
  return (
    <div>
      <input value={t} onChange={(e) => setT(e.target.value)} placeholder="待办" />
      <button>添加</button>
      <ul>{todos.map((x, i) => (<li key={i}>{x}</li>))}</ul>
    </div>
  )
}
"""

_BAD_ROUTER = """// ===== src/main.tsx =====
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import { createRoot } from 'react-dom/client'
createRoot(document.getElementById('root')!).render(
  <BrowserRouter><Routes><Route path="/" element={<div>home</div>} /></Routes></BrowserRouter>
)
"""

# label: "good" expects NOT blocked; "bad" expects blocked.
FIXTURES = [
    {
        "name": "good-todo", "label": "good", "source": _GOOD_TODO,
        "reqs": [("FR1", "用户能新增待办"), ("FR2", "用户能删除待办")],
        "files": {"src/App.tsx": _GOOD_TODO},
        "runtime": {"ran": True, "console_errors": [], "page_errors": [],
                    "interactions": {"total": 2, "clicked": 2, "filled": 1, "dead_controls": []}},
    },
    {
        "name": "bad-dead-control", "label": "bad", "source": _BAD_DEAD,
        "reqs": [("FR1", "用户能新增待办"), ("FR2", "用户能删除待办")],
        "files": {"src/App.tsx": _BAD_DEAD},
        "runtime": {"ran": True, "console_errors": [], "page_errors": [],
                    "interactions": {"total": 1, "clicked": 1, "filled": 1, "dead_controls": ["添加"]}},
    },
    {
        "name": "bad-browser-router", "label": "bad", "source": _BAD_ROUTER,
        "reqs": [("FR1", "首页可访问且子路径预览不跳主域名")],
        "files": {"src/main.tsx": _BAD_ROUTER},
        "runtime": {"ran": True, "console_errors": [], "page_errors": [], "interactions": {}},
    },
]


def evaluate_fixture(fx: dict) -> tuple[dict | None, bool]:
    """Run the real evaluator on one fixture; return (review, blocked)."""
    from backend.services.agent.workflows import _verify_support
    from backend.services.code.frontend_project_service import get_frontend_project_service

    svc = get_frontend_project_service()
    registry = "\n".join(f"- [{rid}] {stmt}" for rid, stmt in fx["reqs"])
    feats = [{"id": rid, "category": "functional", "description": stmt, "passes": False, "note": ""}
             for rid, stmt in fx["reqs"]]
    hr_report = house_rules.render_report(house_rules.check_frontend(fx["files"]))
    rt_report = _verify_support.render_runtime_report(fx.get("runtime"))
    review = svc.review_project(
        source_digest=fx["source"],
        requirements_registry=registry,
        features_block=_verify_support.render_features_block(feats),
        house_rules_report=hr_report,
        runtime_report=rt_report,
    )
    blocked = bool(review and (
        str(review.get("verdict", "")).upper() == "FAIL" or (review.get("blocking_issues") or [])
    ))
    return review, blocked


def run_eval() -> list[dict]:
    out = []
    for fx in FIXTURES:
        review, blocked = evaluate_fixture(fx)
        expect_block = fx["label"] == "bad"
        correct = (review is not None) and (blocked == expect_block)
        out.append({"name": fx["name"], "label": fx["label"], "blocked": blocked,
                    "expect_block": expect_block, "correct": correct,
                    "verdict": (review or {}).get("verdict"), "review": review})
    return out


def main() -> int:
    from backend.services.ai import get_text_provider

    if not (get_text_provider() and get_text_provider().is_configured()):
        print("✗ text provider not configured (AI_TEXT_* / etc.) — cannot run eval.", file=sys.stderr)
        return 2
    results = run_eval()
    correct = sum(1 for r in results if r["correct"])
    for r in results:
        mark = "✓" if r["correct"] else "✗"
        print(f"  {mark} {r['name']:20s} label={r['label']:4s} verdict={r['verdict']!s:9s} "
              f"blocked={r['blocked']} (expect {r['expect_block']})")
    print(f"\nEvaluator discrimination: {correct}/{len(results)} fixtures correct.")
    return 0 if correct == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
