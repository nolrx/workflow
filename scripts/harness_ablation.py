#!/usr/bin/env python3
"""
Harness ablation — "is each gate component still load-bearing?" (Anthropic Article 1's
assumption stress-testing / simplification-first, P0-B's offline companion).

Every gate component (the deterministic house-rules linter, the runtime browser
smoke, the rubric evaluator) encodes an assumption about what the model can't do on
its own. As the model improves, some assumptions stop being load-bearing — and dead
scaffolding is cost + risk. This tool MEASURES each component's marginal contribution
on the labeled eval fixtures: it runs the real evaluator ONCE per fixture, then
recombines the verdict with each component removed (cheap, deterministic — no extra
model calls) and reports, per component:

  * flips   — fixtures whose gate decision (blocked?) changes when it is removed
  * breaks  — fixtures the gate gets RIGHT with it but WRONG without it
               (i.e. the component is load-bearing for that fixture)

A component with 0 breaks across a representative fixture set is a candidate to
simplify away (re-measure on the live model before doing so).

    uv run python scripts/harness_ablation.py
    uv run python scripts/harness_ablation.py --panel 3 --lane backend
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = _SCRIPTS_DIR.parent
for _p in (str(REPO_ROOT), str(_SCRIPTS_DIR)):  # repo root for backend.*, scripts dir for eval_review
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

import eval_review  # noqa: E402  (sibling script in scripts/, now on sys.path)


def _blocked(house, runtime, review) -> bool:
    """Re-derive the gate decision from a component subset (deterministic)."""
    from backend.services.agent.workflows import _verify_support

    return _verify_support.Verification(
        house_rule_errors=house, runtime_errors=runtime, review=review,
    ).blocking


# Each variant zeroes out ONE component while keeping the others as observed.
_COMPONENTS = ("house_rules", "runtime_smoke", "rubric_review")


def _variant_blocked(verification, drop: str) -> bool:
    house = [] if drop == "house_rules" else verification.house_rule_errors
    runtime = [] if drop == "runtime_smoke" else verification.runtime_errors
    review = None if drop == "rubric_review" else verification.review
    return _blocked(house, runtime, review)


def run_ablation(panel: int = 1, lanes=("frontend", "backend")) -> dict:
    plan = []
    if "frontend" in lanes:
        plan += [("frontend", fx) for fx in eval_review.FE_FIXTURES]
    if "backend" in lanes:
        plan += [("backend", fx) for fx in eval_review.BE_FIXTURES]

    tally = {c: {"flips": 0, "breaks": 0, "broken_fixtures": []} for c in _COMPONENTS}
    rows = []
    for lane, fx in plan:
        evaluate = eval_review.evaluate_fe if lane == "frontend" else eval_review.evaluate_be
        _review, verification = evaluate(fx, panel)
        expect = fx["label"] == "bad"
        full = bool(verification.blocking)
        full_correct = full == expect
        row = {"name": fx["name"], "lane": lane, "expect_block": expect, "full_blocked": full}
        for comp in _COMPONENTS:
            without = _variant_blocked(verification, comp)
            row[comp] = without
            if without != full:
                tally[comp]["flips"] += 1
            if full_correct and (without != expect):
                tally[comp]["breaks"] += 1
                tally[comp]["broken_fixtures"].append(f"{fx['name']}({lane})")
        rows.append(row)
    return {"rows": rows, "tally": tally, "panel": panel}


def print_report(result: dict) -> int:
    print(f"\n=== Harness ablation (panel={result['panel']}) ===")
    print("fixture                 lane     expect  full   -house -runtime -review")
    for r in result["rows"]:
        print(f"  {r['name']:22s} {r['lane'][:8]:8s} {str(r['expect_block']):6s} "
              f"{str(r['full_blocked']):6s} {str(r['house_rules']):6s} "
              f"{str(r['runtime_smoke']):8s} {str(r['rubric_review']):6s}")
    print("\n--- marginal contribution (breaks = correct WITH, wrong WITHOUT) ---")
    for comp, t in result["tally"].items():
        note = (" load-bearing for: " + ", ".join(t["broken_fixtures"])) if t["broken_fixtures"] else \
            " (0 breaks on this fixture set — candidate to simplify; re-measure on the live model)"
        print(f"  {comp:14s} flips={t['flips']:2d} breaks={t['breaks']:2d}{note}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Ablate gate components on the eval fixtures.")
    parser.add_argument("--panel", type=int, default=int(os.getenv("CODE_REVIEW_PANEL", "1") or 1))
    parser.add_argument("--lane", choices=["frontend", "backend", "both"], default="both")
    args = parser.parse_args(argv)

    from backend.services.ai import get_text_provider

    if not (get_text_provider() and get_text_provider().is_configured()):
        print("✗ text provider not configured (AI_TEXT_* / etc.) — cannot run ablation.", file=sys.stderr)
        return 2

    lanes = ("frontend", "backend") if args.lane == "both" else (args.lane,)
    return print_report(run_ablation(panel=args.panel, lanes=lanes))


if __name__ == "__main__":
    raise SystemExit(main())
