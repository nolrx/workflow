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


def test_render_runtime_report():
    assert vs.render_runtime_report([]) == ""
    rep = vs.render_runtime_report(["TypeError: undefined"])
    assert "运行时冒烟" in rep and "TypeError" in rep


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


def test_to_record_shape():
    rec = vs.Verification(runtime_errors=["x"]).to_record()
    assert rec["blocking"] is True
    assert "house_rules" in rec and "feature_stats" in rec and "runtime_errors" in rec
