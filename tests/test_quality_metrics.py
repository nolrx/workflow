"""
Unit tests for the eval framework's pure pieces (no network, no DB).

Covers the aggregation math (``summarize_samples`` / ``bucketize`` /
``timeseries_by_day``), the block-reason derivation, the canonical
``weighted_score_of`` helper, and the panel aggregate's weighted_score — i.e.
everything the trend endpoint and the (future) score gate depend on, exercised
without touching a model or a database.
"""
from backend.services.agent.workflows import _verify_support
from backend.services.code import quality_metrics as qm


# --- weighted_score_of ------------------------------------------------------
def test_weighted_score_prefers_model_value():
    assert _verify_support.weighted_score_of({"weighted_score": 3.7, "scores": {}}) == 3.7


def test_weighted_score_recomputed_from_scores():
    review = {"scores": {"design_quality": 4, "originality": 4, "craft": 4, "functionality": 4}}
    # all dims 4 -> weighted average 4.0 regardless of the weight split
    assert _verify_support.weighted_score_of(review) == 4.0


def test_weighted_score_normalises_partial_dims():
    # only functionality present -> normalised over the dims actually scored
    assert _verify_support.weighted_score_of({"scores": {"functionality": 2}}) == 2.0


def test_weighted_score_none_when_no_data():
    assert _verify_support.weighted_score_of(None) is None
    assert _verify_support.weighted_score_of({"scores": {}}) is None
    assert _verify_support.weighted_score_of({}) is None


# --- aggregate_reviews emits weighted_score ---------------------------------
def test_panel_aggregate_emits_weighted_score():
    r1 = {"verdict": "PASS", "scores": {"design_quality": 4, "originality": 4, "craft": 4,
                                        "functionality": 4}, "weighted_score": 4.0,
          "feature_results": [{"id": "FR1", "passes": True}]}
    r2 = {"verdict": "CONCERNS", "scores": {"design_quality": 2, "originality": 2, "craft": 2,
                                            "functionality": 2}, "weighted_score": 2.0,
          "feature_results": [{"id": "FR1", "passes": True}]}
    agg = _verify_support.aggregate_reviews([r1, r2])
    # averaged scores are all 3 -> weighted average 3.0
    assert agg["weighted_score"] == 3.0
    assert agg["scores"]["functionality"] == 3.0


def test_single_review_passthrough_keeps_weighted_score():
    r = {"verdict": "PASS", "weighted_score": 4.2, "scores": {"functionality": 4}}
    assert _verify_support.aggregate_reviews([r])["weighted_score"] == 4.2


# --- block_reasons_of -------------------------------------------------------
def _verif(**kw):
    return _verify_support.Verification(**kw)


def test_block_reasons_house_and_review():
    v = _verif(house_rule_errors=["x"], review={"blocking_issues": ["broken"]})
    assert qm.block_reasons_of(v) == ["house_rule", "review"]


def test_block_reasons_runtime_only():
    v = _verif(runtime_errors=["TypeError: boom"])
    assert qm.block_reasons_of(v) == ["runtime"]


def test_block_reasons_empty_when_clean():
    v = _verif(review={"blocking_issues": []})
    assert qm.block_reasons_of(v) == []


# --- summarize_samples ------------------------------------------------------
def _sample(**kw):
    base = {"blocking": False, "weighted_score": None, "verify_rounds": 1,
            "degraded_reason": None, "feature_passed": 0, "feature_total": 0,
            "verdict": None, "correct": None, "created_at": None}
    base.update(kw)
    return base


def test_summarize_empty():
    out = qm.summarize_samples([])
    assert out["count"] == 0 and out["pass_rate"] is None


def test_summarize_pass_rate_and_means():
    rows = [
        _sample(blocking=False, weighted_score=4.0, verify_rounds=1, verdict="PASS",
                feature_passed=2, feature_total=2),
        _sample(blocking=False, weighted_score=3.0, verify_rounds=2, verdict="CONCERNS",
                feature_passed=1, feature_total=2),
        _sample(blocking=True, weighted_score=1.0, verify_rounds=3, verdict="FAIL",
                degraded_reason="fallback", feature_passed=0, feature_total=2),
    ]
    out = qm.summarize_samples(rows)
    assert out["count"] == 3
    assert out["pass_rate"] == round(1 - 1 / 3, 3)        # one of three blocked
    assert out["mean_weighted_score"] == round((4 + 3 + 1) / 3, 2)
    assert out["mean_verify_rounds"] == round((1 + 2 + 3) / 3, 2)
    assert out["degraded_rate"] == round(1 / 3, 3)
    assert out["feature_pass_rate"] == round(3 / 6, 3)
    assert out["verdicts"] == {"PASS": 1, "CONCERNS": 1, "FAIL": 1}


def test_summarize_eval_accuracy():
    rows = [_sample(correct=True), _sample(correct=True), _sample(correct=False)]
    assert qm.summarize_samples(rows)["eval_accuracy"] == round(2 / 3, 3)


# --- bucketize / timeseries -------------------------------------------------
def test_bucketize_by_lane_skips_empty_key():
    rows = [_sample(lane="frontend"), _sample(lane="frontend"), _sample(lane="backend"),
            _sample(lane=None)]
    buckets = qm.bucketize(rows, "lane")
    assert set(buckets) == {"frontend", "backend"}
    assert buckets["frontend"]["count"] == 2


def test_timeseries_groups_by_day_sorted():
    rows = [_sample(created_at="2026-06-02T10:00:00Z"),
            _sample(created_at="2026-06-01T09:00:00Z"),
            _sample(created_at="2026-06-02T11:00:00Z")]
    ts = qm.timeseries_by_day(rows)
    assert [d["day"] for d in ts] == ["2026-06-01", "2026-06-02"]
    assert ts[1]["count"] == 2
