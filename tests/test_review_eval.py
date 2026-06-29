"""
Integration eval for the skeptical project evaluator (②b).

Runs the REAL evaluator on labeled good/bad fixtures (see scripts/eval_review.py)
and asserts it discriminates: blocks broken builds, doesn't fabricate FAILs on a
working app. Marked ``integration`` (hits the live text lane) so it is excluded
from the default ``pytest -m "not integration"`` run; it is the regression guard
for the critic prompt's judgment quality.

    uv run pytest -m integration tests/test_review_eval.py -s
"""
import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "eval_review", Path(__file__).resolve().parent.parent / "scripts" / "eval_review.py"
)
eval_review = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(eval_review)


@pytest.mark.integration
def test_evaluator_discriminates_good_from_bad():
    from backend.services.ai import get_text_provider

    provider = get_text_provider()
    if not (provider and provider.is_configured()):
        pytest.skip("text provider not configured (AI_TEXT_* etc.)")

    results = eval_review.run_eval()

    for r in results:
        assert r["review"] is not None, f"{r['name']}: evaluator returned nothing"

    # Broken builds MUST be blocked (verdict FAIL or non-empty blocking_issues).
    for r in (x for x in results if x["label"] == "bad"):
        assert r["blocked"], (
            f"{r['name']}: skeptical evaluator failed to block a broken build "
            f"(verdict={r['verdict']!r})"
        )

    # A working app must NOT be hard-FAILed (self-praise is fine to NOT do, but a
    # fabricated FAIL on clean code means the evaluator is mis-calibrated).
    for r in (x for x in results if x["label"] == "good"):
        assert str(r["verdict"]).upper() != "FAIL", (
            f"{r['name']}: evaluator fabricated a FAIL on a working app"
        )
