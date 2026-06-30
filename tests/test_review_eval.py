"""
Integration eval for the skeptical project evaluator (eval framework, P0-B).

Runs the REAL evaluator on labeled good/bad fixtures (see scripts/eval_review.py)
through the SAME verify gate the live workflows use, and asserts it discriminates:
blocks broken builds, doesn't block / FAIL a working app. Covers BOTH the frontend
and backend lanes. Marked ``integration`` (hits the live text lane) so it is
excluded from the default ``pytest -m "not integration"`` run; it is the regression
guard for the critic prompts' judgment quality.

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
    assert results, "no fixtures evaluated"

    # A None review = the judge model was UNAVAILABLE (402 / timeout), NOT a judgment.
    # Only assert on fixtures the judge actually judged; skip if the lane is fully down
    # (a billing/infra outage is not a critic-prompt regression).
    judged = [r for r in results if r["review"] is not None]
    if not judged:
        pytest.skip("evaluator returned nothing for ALL fixtures — text lane down (402/timeout)")

    for r in judged:
        # Every successful review must carry a gradable weighted_score (the score gate needs it).
        assert isinstance(r["weighted_score"], (int, float)), (
            f"{r['name']}: review has no numeric weighted_score"
        )

    # Broken builds MUST be blocked by the end-to-end gate (house rule / runtime /
    # the evaluator's blocking_issues).
    for r in (x for x in judged if x["label"] == "bad"):
        assert r["blocked"], (
            f"{r['name']} ({r['lane']}): gate failed to block a broken build "
            f"(verdict={r['verdict']!r})"
        )

    # A working app must NOT be blocked, nor hard-FAILed (a fabricated FAIL / block on
    # clean code means the evaluator is mis-calibrated toward over-blocking).
    for r in (x for x in judged if x["label"] == "good"):
        assert not r["blocked"], (
            f"{r['name']} ({r['lane']}): gate blocked a working app "
            f"(blocking_issues={((r['review'] or {}).get('blocking_issues') or [])[:3]})"
        )
        assert str(r["verdict"]).upper() != "FAIL", (
            f"{r['name']} ({r['lane']}): evaluator fabricated a FAIL on a working app"
        )
