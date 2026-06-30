"""
Unit tests for the P0-A verify gate (score threshold + refine/pivot), no network/DB.

Covers ``Verification.score_blocking`` / ``blocking`` under the A1 score gate, the
repair-brief quality feedback, the A3 ``should_pivot`` stall detector, and the env
parsers — i.e. the judgment-changing logic, all exercised offline so the default-OFF
guarantee (gate behaviour identical to pre-A1 when no env is set) is pinned.
"""
from backend.services.agent.workflows import _verify_support
from backend.services.agent.workflows._verify_support import Verification
from backend.services.code import quality_metrics as qm


def _review(ws=None, scores=None, blocking=None, advisory=None):
    r = {}
    if ws is not None:
        r["weighted_score"] = ws
    if scores is not None:
        r["scores"] = scores
    if blocking is not None:
        r["blocking_issues"] = blocking
    if advisory is not None:
        r["advisory_issues"] = advisory
    return r


# --- A1: default OFF preserves behaviour -------------------------------------
def test_gate_off_by_default_clean_build_not_blocking():
    v = Verification(review=_review(ws=1.0))  # very low score but NO floor configured
    assert v.score_blocking == []
    assert v.blocking is False


def test_gate_off_still_blocks_on_objective_defect():
    v = Verification(review=_review(ws=5.0, blocking=["broken button"]))
    assert v.blocking is True  # objective blocker, unrelated to score gate


# --- A1: score floor bites when configured -----------------------------------
def test_score_floor_blocks_below():
    v = Verification(review=_review(ws=1.6), min_weighted_score=2.5)
    assert v.score_blocking and "1.6" in v.score_blocking[0]
    assert v.blocking is True


def test_score_floor_passes_at_or_above():
    v = Verification(review=_review(ws=2.5), min_weighted_score=2.5)
    assert v.score_blocking == []
    assert v.blocking is False


def test_score_floor_recomputes_when_model_omits_weighted_score():
    # only per-dim scores present -> weighted_score_of recomputes (all 4 -> 4.0)
    v = Verification(review=_review(scores={"design_quality": 4, "originality": 4,
                                            "craft": 4, "functionality": 4}),
                     min_weighted_score=3.0)
    assert v.weighted_score == 4.0
    assert v.score_blocking == []


def test_per_dim_floor_blocks():
    v = Verification(
        review=_review(scores={"design_quality": 4, "originality": 4, "craft": 4,
                               "functionality": 1}),
        min_dim_scores={"functionality": 3.0},
    )
    assert v.score_blocking and "functionality" in v.score_blocking[0]
    assert v.blocking is True


def test_score_gate_never_penalises_unjudged_build():
    # review None (judge down) -> score gate stays silent even with a floor set
    v = Verification(review=None, min_weighted_score=4.0)
    assert v.score_blocking == []
    assert v.blocking is False


# --- block-reason wiring (records "threshold") -------------------------------
def test_block_reasons_include_threshold():
    v = Verification(review=_review(ws=1.0), min_weighted_score=2.5)
    assert qm.block_reasons_of(v) == ["threshold"]


def test_block_reasons_combine_objective_and_threshold():
    v = Verification(house_rule_errors=["x"], review=_review(ws=1.0, blocking=["b"]),
                     min_weighted_score=2.5)
    assert qm.block_reasons_of(v) == ["house_rule", "review", "threshold"]


# --- repair brief folds in quality feedback ----------------------------------
def test_repair_instruction_adds_quality_section_when_score_blocked():
    v = Verification(review=_review(ws=1.0, advisory=["留白偏紧", "配色单调"]),
                     min_weighted_score=2.5)
    brief = v.repair_instruction()
    assert "质量未达阈值" in brief
    assert "留白偏紧" in brief  # advisory polish items promoted into the repair brief


def test_repair_instruction_no_quality_section_when_gate_off():
    v = Verification(review=_review(ws=1.0, advisory=["留白偏紧"]))
    assert "质量未达阈值" not in v.repair_instruction()


# --- A3: refine -> pivot stall detector --------------------------------------
def test_should_pivot_needs_two_scores():
    assert _verify_support.should_pivot([]) is False
    assert _verify_support.should_pivot([3.0]) is False


def test_should_pivot_true_when_stalled():
    assert _verify_support.should_pivot([2.0, 2.0]) is True       # no gain
    assert _verify_support.should_pivot([2.0, 2.1]) is True       # gain < 0.2


def test_should_pivot_false_when_improving():
    assert _verify_support.should_pivot([2.0, 2.5]) is False      # gain >= 0.2


# --- env parsers -------------------------------------------------------------
def test_env_score_floor(monkeypatch):
    monkeypatch.delenv("CODE_QUALITY_MIN_SCORE", raising=False)
    assert _verify_support.env_score_floor() is None
    monkeypatch.setenv("CODE_QUALITY_MIN_SCORE", "2.5")
    assert _verify_support.env_score_floor() == 2.5
    monkeypatch.setenv("CODE_QUALITY_MIN_SCORE", "not-a-number")
    assert _verify_support.env_score_floor() is None


def test_env_dim_floors(monkeypatch):
    for env in ("CODE_QUALITY_MIN_DESIGN", "CODE_QUALITY_MIN_ORIGINALITY",
                "CODE_QUALITY_MIN_CRAFT", "CODE_QUALITY_MIN_FUNCTIONALITY"):
        monkeypatch.delenv(env, raising=False)
    assert _verify_support.env_dim_floors() is None
    monkeypatch.setenv("CODE_QUALITY_MIN_FUNCTIONALITY", "3")
    assert _verify_support.env_dim_floors() == {"functionality": 3.0}


def test_pivot_enabled(monkeypatch):
    monkeypatch.delenv("CODE_REPAIR_PIVOT", raising=False)
    assert _verify_support.pivot_enabled() is False
    monkeypatch.setenv("CODE_REPAIR_PIVOT", "1")
    assert _verify_support.pivot_enabled() is True
    monkeypatch.setenv("CODE_REPAIR_PIVOT", "0")
    assert _verify_support.pivot_enabled() is False


# --- P1-1: repair regression guard -------------------------------------------
def _feat(fid, passes, category="functional"):
    return {"id": fid, "category": category, "passes": passes, "note": ""}


def _verif_with(features=None, house=0, runtime=0):
    return Verification(
        features=features or [],
        house_rule_errors=["e"] * house,    # only len() matters to repair_regressed
        runtime_errors=["r"] * runtime,
    )


def test_repair_regressed_when_passing_feature_breaks():
    prior = _verif_with(features=[_feat("FR1", True), _feat("FR2", True)])
    cand = _verif_with(features=[_feat("FR1", True), _feat("FR2", False)])
    regressed, why = _verify_support.repair_regressed(prior, cand)
    assert regressed and "FR2" in why


def test_repair_regressed_on_new_house_error():
    regressed, why = _verify_support.repair_regressed(_verif_with(house=0), _verif_with(house=1))
    assert regressed and "房规" in why


def test_repair_regressed_on_new_runtime_error():
    assert _verify_support.repair_regressed(_verif_with(runtime=0), _verif_with(runtime=1))[0] is True


def test_repair_not_regressed_when_improved():
    # repair flips FR2 fail->pass AND clears a house error -> NOT a regression
    prior = _verif_with(features=[_feat("FR1", True), _feat("FR2", False)], house=1)
    cand = _verif_with(features=[_feat("FR1", True), _feat("FR2", True)], house=0)
    regressed, why = _verify_support.repair_regressed(prior, cand)
    assert regressed is False and why == ""


def test_repair_not_regressed_when_unchanged():
    v = [_feat("FR1", True)]
    assert _verify_support.repair_regressed(_verif_with(features=v), _verif_with(features=v))[0] is False


def test_repair_not_regressed_ignores_nonfunctional_and_soft_score():
    # an NFR flipping or a lower polish score is not a hard regression
    prior = _verif_with(features=[_feat("NFR1", True, category="non_functional")])
    cand = _verif_with(features=[_feat("NFR1", False, category="non_functional")])
    assert _verify_support.repair_regressed(prior, cand)[0] is False


# --- review-panel concurrency (#1) -------------------------------------------
def test_run_reviewers_runs_all_in_order():
    out = _verify_support.run_reviewers([lambda i=i: {"i": i} for i in range(3)])
    assert out == [{"i": 0}, {"i": 1}, {"i": 2}]


def test_run_reviewers_single_thunk():
    assert _verify_support.run_reviewers([lambda: {"verdict": "FAIL"}]) == [{"verdict": "FAIL"}]


def test_run_reviewers_swallows_exceptions_preserving_order():
    def boom():
        raise RuntimeError("reviewer crashed")

    out = _verify_support.run_reviewers([lambda: {"ok": 1}, boom, lambda: {"ok": 2}])
    assert out == [{"ok": 1}, None, {"ok": 2}]


def test_run_reviewers_empty():
    assert _verify_support.run_reviewers([]) == []


# --- P-A: acceptance-driven iteration (should_stop + env + coverage) ----------
def _verif(features=None, blocking=False):
    # blocking is driven purely by a house-rule error here (Verification.blocking
    # ORs the objective signals; review/score gates stay None/empty in these tests).
    return Verification(features=features or [], house_rule_errors=(["x"] if blocking else []))


def test_iterate_envs_default_off(monkeypatch):
    for e in ("CODE_ITERATE_TO_ACCEPTANCE", "CODE_ITERATE_MAX_ROUNDS", "CODE_ITERATE_STALL"):
        monkeypatch.delenv(e, raising=False)
    assert _verify_support.iterate_to_acceptance() is False
    assert _verify_support.iterate_max_rounds(2) == 2   # unset -> falls back to verify default
    assert _verify_support.iterate_max_rounds(5) == 5
    assert _verify_support.iterate_stall() == 2


def test_iterate_envs_when_set(monkeypatch):
    monkeypatch.setenv("CODE_ITERATE_TO_ACCEPTANCE", "1")
    monkeypatch.setenv("CODE_ITERATE_MAX_ROUNDS", "6")
    monkeypatch.setenv("CODE_ITERATE_STALL", "3")
    assert _verify_support.iterate_to_acceptance() is True
    assert _verify_support.iterate_max_rounds(2) == 6
    assert _verify_support.iterate_stall() == 3


def test_iterate_max_rounds_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("CODE_ITERATE_MAX_ROUNDS", "nope")
    assert _verify_support.iterate_max_rounds(2) == 2


def test_functional_coverage_counts_functional_only():
    feats = [_feat("FR1", True), _feat("FR2", False),
             _feat("NFR1", True, category="non_functional")]
    assert _verify_support.functional_coverage(feats) == (1, 2)


# should_stop — DEFAULT mode must equal the legacy "not blocking or round>=max" break.
def test_should_stop_default_matches_legacy():
    assert _verify_support.should_stop(_verif(blocking=False), 0, 2)[0] is True   # not blocking
    assert _verify_support.should_stop(_verif(blocking=True), 0, 2)[0] is False   # blocking -> repair
    assert _verify_support.should_stop(_verif(blocking=True), 2, 2)[0] is True    # round cap
    assert _verify_support.should_stop(_verif(blocking=False), 2, 2)[0] is True


# should_stop — ACCEPTANCE mode.
def test_should_stop_acceptance_continues_while_features_unmet():
    v = _verif(features=[_feat("FR1", True), _feat("FR2", False)], blocking=False)
    stop, _ = _verify_support.should_stop(v, 0, 6, to_acceptance=True,
                                          coverage_history=[1], stall=2)
    assert stop is False  # not blocking but FR2 unmet -> keep iterating


def test_should_stop_acceptance_stops_when_all_pass():
    v = _verif(features=[_feat("FR1", True), _feat("FR2", True)], blocking=False)
    stop, why = _verify_support.should_stop(v, 0, 6, to_acceptance=True,
                                            coverage_history=[2], stall=2)
    assert stop is True and "达标" in why


def test_should_stop_acceptance_respects_budget():
    v = _verif(features=[_feat("FR1", False)], blocking=True)
    stop, why = _verify_support.should_stop(v, 6, 6, to_acceptance=True,
                                            coverage_history=[0], stall=2)
    assert stop is True and "上限" in why


def test_should_stop_acceptance_stalls_on_no_coverage_gain():
    v = _verif(features=[_feat("FR1", False), _feat("FR2", False)], blocking=False)
    stop, why = _verify_support.should_stop(v, 3, 6, to_acceptance=True,
                                            coverage_history=[0, 0, 0], stall=2)
    assert stop is True and "无增长" in why


def test_should_stop_acceptance_not_stalled_when_improving():
    v = _verif(features=[_feat("FR1", True), _feat("FR2", False)], blocking=False)
    stop, _ = _verify_support.should_stop(v, 2, 6, to_acceptance=True,
                                          coverage_history=[0, 1, 2], stall=2)
    assert stop is False  # coverage rising and FR2 still unmet -> continue


# --- P-B: incremental batched build (split + env + brief) --------------------
def test_build_batches_default_and_set(monkeypatch):
    monkeypatch.delenv("CODE_BUILD_BATCHES", raising=False)
    assert _verify_support.build_batches() == 1
    monkeypatch.setenv("CODE_BUILD_BATCHES", "4")
    assert _verify_support.build_batches() == 4
    monkeypatch.setenv("CODE_BUILD_BATCHES", "nope")
    assert _verify_support.build_batches() == 1


def test_split_batches_no_batching_cases():
    feats = [_feat(f"FR{i}", False) for i in range(6)]
    assert _verify_support.split_batches(feats, 1) == []          # n<=1 → single build
    assert _verify_support.split_batches([_feat("FR1", False)], 3) == []  # <2 functional
    assert _verify_support.split_batches(feats[:2], 3) == []      # fewer features than batches


def test_split_batches_contiguous_chunks_functional_only():
    feats = ([_feat(f"FR{i}", False) for i in range(10)]
             + [_feat("NFR1", False, category="non_functional")])
    batches = _verify_support.split_batches(feats, 4)
    assert len(batches) == 4
    # contiguous, order-preserving, functional-only (NFR excluded), no feature lost
    flat = [f["id"] for b in batches for f in b]
    assert flat == [f"FR{i}" for i in range(10)]
    assert all(f["category"] == "functional" for b in batches for f in b)


def test_render_feature_subset_lists_batch():
    brief = _verify_support.render_feature_subset([_feat("FR3", False), _feat("FR4", False)], 1, 3)
    assert "第 2/3 批" in brief and "[FR3]" in brief and "[FR4]" in brief
