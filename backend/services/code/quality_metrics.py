"""
Generation-quality metrics (the eval framework's online half, P0-B).

Two jobs:

  * **record** one ``CodeQualitySample`` at the end of a generation run's
    verify->repair loop (``record_quality_sample``) or for an offline eval fixture
    (``record_eval_sample``). Writes are FAIL-SOFT — a metrics write must never
    break a generation run.
  * **summarize** stored samples into success-rate / mean-score / repair-round
    trends (``summarize_quality``), powering ``GET /api/code/quality/trends``.

The aggregation math lives in PURE functions (``summarize_samples`` / ``bucketize``
/ ``timeseries_by_day``) that operate on plain dicts, so they are unit-testable
without a database or network (see tests/test_quality_metrics.py).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from backend.extensions import db

logger = logging.getLogger(__name__)


# --- pure aggregation (no DB / network) -------------------------------------
def _mean(values: list) -> float | None:
    nums = [float(v) for v in values if isinstance(v, (int, float))]
    return round(sum(nums) / len(nums), 2) if nums else None


def summarize_samples(samples: list[dict]) -> dict:
    """Aggregate a list of CodeQualitySample.to_dict() rows into headline metrics.

    ``pass_rate`` = fraction whose FINAL verify round was NOT blocking (i.e. the
    build was accepted without an unresolved objective defect). ``degraded_rate``
    and ``feature_pass_rate`` complement it. For ``eval`` rows, ``eval_accuracy``
    is the discrimination rate (blocked == expected).
    """
    n = len(samples)
    if not n:
        return {
            "count": 0, "pass_rate": None, "mean_weighted_score": None,
            "mean_verify_rounds": None, "degraded_rate": None,
            "feature_pass_rate": None, "verdicts": {}, "eval_accuracy": None,
        }
    blocked = sum(1 for s in samples if s.get("blocking"))
    degraded = sum(1 for s in samples if s.get("degraded_reason"))
    feat_pass = sum((s.get("feature_passed") or 0) for s in samples)
    feat_total = sum((s.get("feature_total") or 0) for s in samples)
    verdicts: dict[str, int] = {}
    for s in samples:
        key = s.get("verdict") or "—"
        verdicts[key] = verdicts.get(key, 0) + 1
    evals = [s for s in samples if s.get("correct") is not None]
    return {
        "count": n,
        "pass_rate": round(1 - blocked / n, 3),
        "blocked": blocked,
        "mean_weighted_score": _mean([s.get("weighted_score") for s in samples]),
        "mean_verify_rounds": _mean([s.get("verify_rounds") for s in samples]),
        "degraded_rate": round(degraded / n, 3),
        "feature_pass_rate": round(feat_pass / feat_total, 3) if feat_total else None,
        "verdicts": verdicts,
        "eval_accuracy": round(sum(1 for s in evals if s.get("correct")) / len(evals), 3)
        if evals else None,
    }


def bucketize(samples: list[dict], key: str) -> dict:
    """Group rows by ``row[key]`` and summarize each bucket (skips empty keys)."""
    buckets: dict[str, list[dict]] = {}
    for s in samples:
        val = s.get(key)
        if val in (None, ""):
            continue
        buckets.setdefault(str(val), []).append(s)
    return {k: summarize_samples(v) for k, v in buckets.items()}


def timeseries_by_day(samples: list[dict]) -> list[dict]:
    """Daily buckets (UTC date prefix of created_at), oldest first."""
    by_day: dict[str, list[dict]] = {}
    for s in samples:
        created = s.get("created_at") or ""
        day = created[:10]
        if not day:
            continue
        by_day.setdefault(day, []).append(s)
    return [{"day": d, **summarize_samples(by_day[d])} for d in sorted(by_day)]


# --- block-reason derivation (pure) -----------------------------------------
def block_reasons_of(verification) -> list[str]:
    """Which objective signal(s) the final verdict blocked on — for the sample.

    ``threshold`` (the score gate) is read defensively so this keeps working
    whether or not the A1 score-gate has landed on ``Verification`` yet.
    """
    reasons: list[str] = []
    if getattr(verification, "house_rule_errors", None):
        reasons.append("house_rule")
    if getattr(verification, "runtime_errors", None):
        reasons.append("runtime")
    if verification.review_blocking:
        reasons.append("review")
    if getattr(verification, "score_blocking", None):  # A1 score gate, if present
        reasons.append("threshold")
    return reasons


# --- persistence (fail-soft) ------------------------------------------------
def _text_model_name() -> str | None:
    try:
        from backend.services.ai import get_text_provider

        provider = get_text_provider()
        return getattr(provider, "model", None) if provider else None
    except Exception:  # noqa: BLE001
        return None


def record_quality_sample(
    *, run_id, project_id, user_id, team_id, lane, verification,
    verify_rounds, degraded_reason=None, model_name=None, prompt_version=None,
):
    """Persist ONE online quality sample from a finished verify->repair loop.

    Returns the row, or ``None`` if anything went wrong — a metrics write must
    never break a generation run, so all errors are swallowed (rolled back).
    """
    try:
        from backend.models.code.quality import CodeQualitySample, QualitySampleKind
        from backend.services.agent.workflows import _verify_support

        review = verification.review if isinstance(verification.review, dict) else {}
        scores = review.get("scores") if isinstance(review.get("scores"), dict) else {}
        stats = verification.feature_stats or {}
        panel = review.get("panel") if isinstance(review.get("panel"), dict) else {}
        sample = CodeQualitySample(
            run_id=run_id, project_id=project_id, user_id=user_id, team_id=team_id,
            kind=QualitySampleKind.ONLINE, lane=lane,
            verdict=(str(review.get("verdict")).upper() if review.get("verdict") else None),
            weighted_score=_verify_support.weighted_score_of(review),
            feature_passed=stats.get("passed"), feature_total=stats.get("total"),
            blocking=bool(verification.blocking),
            verify_rounds=verify_rounds, degraded_reason=degraded_reason,
            panel_n=panel.get("n"), panel_flagging=panel.get("flagging_blocking"),
            prompt_version=prompt_version, model_name=model_name or _text_model_name(),
        )
        sample.set_scores(scores)
        sample.set_block_reasons(block_reasons_of(verification))
        db.session.add(sample)
        db.session.commit()
        return sample
    except Exception:  # noqa: BLE001 — metrics must never sink a run
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("quality sample (online) skipped", exc_info=True)
        return None


def record_eval_sample(
    *, lane, fixture_name, review, blocked, expected_block, correct,
    user_id=None, prompt_version=None, model_name=None,
):
    """Persist ONE offline eval observation (regression guard for the critic)."""
    try:
        from backend.models.code.quality import CodeQualitySample, QualitySampleKind
        from backend.services.agent.workflows import _verify_support

        review = review or {}
        scores = review.get("scores") if isinstance(review.get("scores"), dict) else {}
        sample = CodeQualitySample(
            user_id=user_id, kind=QualitySampleKind.EVAL, lane=lane,
            verdict=(str(review.get("verdict")).upper() if review.get("verdict") else None),
            weighted_score=_verify_support.weighted_score_of(review),
            blocking=bool(blocked),
            fixture_name=fixture_name, expected_block=bool(expected_block),
            correct=bool(correct),
            prompt_version=prompt_version, model_name=model_name or _text_model_name(),
        )
        sample.set_scores(scores)
        db.session.add(sample)
        db.session.commit()
        return sample
    except Exception:  # noqa: BLE001
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("quality sample (eval) skipped", exc_info=True)
        return None


def record_doc_quality_sample(*, run_id, project_id, user_id, team_id, completed, conflicts):
    """Persist ONE quality sample for the doc pipeline (``code_full_generation``).

    That 7-step workflow has no rubric/functionality score — its quality signal is the
    context-consistency gate (CONTEXT_CONFLICT events) plus completion. ``blocking``
    means the run finished with a context conflict OR did not complete (PARTIAL); the
    rubric/functionality columns stay NULL (N/A for a doc pipeline). Fail-soft.
    """
    try:
        from backend.models.code.quality import CodeQualitySample, QualitySampleKind

        reasons: list[str] = []
        if conflicts:
            reasons.append("context_conflict")
        if not completed:
            reasons.append("incomplete")
        verdict = "FAIL" if conflicts else ("CONCERNS" if not completed else "PASS")
        sample = CodeQualitySample(
            run_id=run_id, project_id=project_id, user_id=user_id, team_id=team_id,
            kind=QualitySampleKind.ONLINE, lane="full_generation",
            verdict=verdict, blocking=bool(reasons), model_name=_text_model_name(),
        )
        sample.set_block_reasons(reasons)
        db.session.add(sample)
        db.session.commit()
        return sample
    except Exception:  # noqa: BLE001 — metrics must never sink a run
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("quality sample (doc) skipped", exc_info=True)
        return None


# --- query + summarize ------------------------------------------------------
def summarize_quality(
    *, lane=None, kind="online", window_days=30,
    team_id=None, project_id=None, user_id=None,
) -> dict:
    """Trend summary over stored samples, sliced by the given filters."""
    from backend.models.code.quality import CodeQualitySample

    query = CodeQualitySample.query.filter(CodeQualitySample.kind == kind)
    if lane:
        query = query.filter(CodeQualitySample.lane == lane)
    if team_id:
        query = query.filter(CodeQualitySample.team_id == team_id)
    if project_id:
        query = query.filter(CodeQualitySample.project_id == project_id)
    if user_id:
        query = query.filter(CodeQualitySample.user_id == user_id)
    if window_days:
        cutoff = datetime.utcnow() - timedelta(days=int(window_days))
        query = query.filter(CodeQualitySample.created_at >= cutoff)

    rows = [s.to_dict() for s in query.order_by(CodeQualitySample.created_at.asc()).all()]
    return {
        "kind": kind,
        "lane": lane,
        "window_days": window_days,
        "overall": summarize_samples(rows),
        "by_lane": bucketize(rows, "lane"),
        "by_prompt_version": bucketize(rows, "prompt_version"),
        "by_model": bucketize(rows, "model_name"),
        "by_day": timeseries_by_day(rows),
    }
