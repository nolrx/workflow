"""Unit tests for the shared verify->repair helpers (no network/Docker)."""
from backend.services.agent.workflows import _verify_support as vs
from backend.services.code import house_rules


def _ledger(reqs):
    return {"requirements": reqs}


def test_features_from_ledger_all_failing_and_categorised():
    feats = vs.features_from_ledger(_ledger([
        {"id": "FR1", "statement": "用户能登录"},
        {"id": "NFR1", "statement": "首屏 2 秒内"},
        {"bad": "no id"},
    ]))
    assert len(feats) == 2
    assert all(f["passes"] is False for f in feats)
    assert feats[0] == {"id": "FR1", "category": "functional", "description": "用户能登录", "passes": False, "note": ""}
    assert feats[1]["category"] == "non_functional"


def test_render_features_block_lists_ids():
    block = vs.render_features_block(vs.features_from_ledger(_ledger([{"id": "FR1", "statement": "登录"}])))
    assert "FR1" in block and "登录" in block


def test_render_features_block_empty():
    assert vs.render_features_block([]) == ""


def test_apply_feature_results_marks_pass_fail():
    feats = vs.features_from_ledger(_ledger([
        {"id": "FR1", "statement": "登录"}, {"id": "FR2", "statement": "注册"},
    ]))
    updated, stats = vs.apply_feature_results(feats, [
        {"id": "FR1", "passes": True, "note": "LoginForm.tsx"},
        {"id": "FR2", "passes": False, "note": "无注册入口"},
    ])
    assert stats == {"total": 2, "passed": 1, "failed": 1}
    assert updated[0]["passes"] is True and updated[0]["note"] == "LoginForm.tsx"
    assert vs.failed_functional_features(updated) == [updated[1]]


def test_runtime_errors_only_when_ran():
    assert vs.runtime_errors(None) == []
    assert vs.runtime_errors({"ran": False, "console_errors": ["x"]}) == []
    assert vs.runtime_errors({"ran": True, "console_errors": ["TypeError: x"], "page_errors": ["boom"]}) == [
        "TypeError: x", "boom"
    ]


def test_render_runtime_report_dict():
    assert vs.render_runtime_report(None) == ""
    assert vs.render_runtime_report({"ran": False}) == ""
    rep = vs.render_runtime_report({
        "ran": True, "console_errors": ["TypeError: undefined"],
        "interactions": {"clicked": 3, "total": 5, "filled": 1},
    })
    assert "TypeError" in rep and "必须修复" in rep
    clean = vs.render_runtime_report({
        "ran": True, "console_errors": [],
        "interactions": {"clicked": 4, "total": 4, "filled": 2, "dead_controls": []},
    })
    assert "未捕获" in clean
    dead = vs.render_runtime_report({
        "ran": True, "console_errors": [],
        "interactions": {"dead_controls": ["保存"], "clicked": 2, "total": 2},
    })
    assert "无效控件" in dead and "保存" in dead


def test_dead_controls_helper():
    assert vs.dead_controls(None) == []
    assert vs.dead_controls({"interactions": {"dead_controls": ["保存", "提交"]}}) == ["保存", "提交"]


def test_interaction_console_error_blocks():
    rc = {"ran": True, "console_errors": ["TypeError x while clicking"], "interactions": {"clicked": 1}}
    v = vs.Verification(runtime_check=rc, runtime_errors=vs.runtime_errors(rc))
    assert v.blocking is True


def test_dead_control_alone_is_not_blocking():
    rc = {"ran": True, "console_errors": [], "page_errors": [],
          "interactions": {"dead_controls": ["保存"], "clicked": 1, "total": 1}}
    v = vs.Verification(runtime_check=rc, runtime_errors=vs.runtime_errors(rc))
    assert v.blocking is False  # a dead control is a soft signal, never hard-blocks
    assert "无效控件" in v.repair_instruction()  # ...but it IS surfaced for fixing
    assert "疑似无效控件" in v.summary_line()


def test_verification_blocking_on_house_rule_error():
    v = house_rules.Violation("fe-no-browser-router", house_rules.SEVERITY_ERROR, "a.tsx", "m", "fix")
    ver = vs.Verification(house_rule_errors=[v])
    assert ver.blocking is True


def test_verification_blocking_on_runtime_error():
    assert vs.Verification(runtime_errors=["boom"]).blocking is True


def test_verification_blocking_on_review_blocking_issues():
    assert vs.Verification(review={"verdict": "FAIL", "blocking_issues": ["core broken"]}).blocking is True


def test_verification_not_blocking_on_subjective_fail_alone():
    # A FAIL verdict with NO blocking_issues is subjective -> must NOT churn repair.
    assert vs.Verification(review={"verdict": "FAIL", "blocking_issues": []}).blocking is False


def test_verification_not_blocking_when_clean():
    assert vs.Verification(review={"verdict": "PASS"}).blocking is False


def test_repair_instruction_aggregates_signals():
    v = house_rules.Violation("fe-no-tailwind", house_rules.SEVERITY_ERROR, "pkg.json", "tailwind dep", "去掉 tailwind")
    feats = vs.features_from_ledger(_ledger([{"id": "FR2", "statement": "注册"}]))
    ver = vs.Verification(
        house_rule_errors=[v],
        runtime_errors=["TypeError: boom"],
        review={"blocking_issues": ["FR1 登录按钮无效"]},
        features=feats,  # FR2 still failing (passes False)
    )
    instr = ver.repair_instruction()
    assert "去掉 tailwind" in instr
    assert "TypeError: boom" in instr
    assert "FR1 登录按钮无效" in instr
    assert "FR2" in instr  # unimplemented functional feature folded in


def test_repair_instruction_handoff_lists_passed_features():
    feats = vs.features_from_ledger(_ledger([
        {"id": "FR1", "statement": "用户能登录"}, {"id": "FR2", "statement": "用户能注册"},
    ]))
    feats, _ = vs.apply_feature_results(feats, [
        {"id": "FR1", "passes": True}, {"id": "FR2", "passes": False},
    ])
    v = vs.Verification(
        house_rule_errors=[house_rules.Violation(
            "fe-no-tailwind", house_rules.SEVERITY_ERROR, "pkg.json", "tailwind", "去掉")],
        features=feats,
    )
    instr = v.repair_instruction()
    assert "续修交接" in instr and "[FR1]" in instr  # passed feature: keep, don't touch
    assert "FR2" in instr  # failed functional feature: implement


def test_to_record_shape():
    rec = vs.Verification(runtime_errors=["x"]).to_record()
    assert rec["blocking"] is True
    assert "house_rules" in rec and "feature_stats" in rec and "runtime_errors" in rec


# --- reviewer panel / consensus (②a) ----------------------------------------
def test_aggregate_reviews_passthrough_and_empty():
    assert vs.aggregate_reviews([]) is None
    assert vs.aggregate_reviews([None]) is None
    one = {"verdict": "PASS", "blocking_issues": []}
    assert vs.aggregate_reviews([one]) is one  # single review unchanged


def test_aggregate_reviews_majority_blocks():
    revs = [
        {"verdict": "FAIL", "blocking_issues": ["登录按钮无效"], "feature_results": [{"id": "FR1", "passes": False}]},
        {"verdict": "FAIL", "blocking_issues": ["保存不持久"], "feature_results": [{"id": "FR1", "passes": False}]},
        {"verdict": "PASS", "blocking_issues": [], "feature_results": [{"id": "FR1", "passes": True}]},
    ]
    agg = vs.aggregate_reviews(revs)
    assert agg["verdict"] == "FAIL"  # 2/3 flagged
    assert agg["panel"]["panel_blocking"] is True
    assert set(agg["blocking_issues"]) == {"登录按钮无效", "保存不持久"}  # union once blocking
    # FR1 failed by majority (2/3) -> not passing
    assert agg["feature_results"][0]["passes"] is False


def test_aggregate_reviews_minority_concern_demoted_to_advisory():
    revs = [
        {"verdict": "FAIL", "blocking_issues": ["可能的边角问题"], "feature_results": [{"id": "FR1", "passes": True}]},
        {"verdict": "PASS", "blocking_issues": [], "feature_results": [{"id": "FR1", "passes": True}]},
        {"verdict": "PASS", "blocking_issues": [], "feature_results": [{"id": "FR1", "passes": True}]},
    ]
    agg = vs.aggregate_reviews(revs)
    assert agg["verdict"] != "FAIL"  # only 1/3 flagged -> not blocking
    assert agg["blocking_issues"] == []  # minority blocking demoted
    assert "可能的边角问题" in agg["advisory_issues"]
    assert agg["feature_results"][0]["passes"] is True  # 3/3 pass


def test_aggregate_reviews_averages_scores():
    revs = [
        {"verdict": "PASS", "scores": {"functionality": 4}},
        {"verdict": "PASS", "scores": {"functionality": 2}},
    ]
    agg = vs.aggregate_reviews(revs)
    assert agg["scores"]["functionality"] == 3.0
