"""Unit tests for the deploy-time repair ladder's pure guard helpers.

These cover the availability-critical predicates added for the always-run
(proactive FE↔BE alignment) repair pass: the meaningful-change detector that lets a
no-op pass skip the rebuild (ignoring lockfile churn), and the regression guard that
must catch a candidate making a healthy app worse — including the ``ok→inconclusive``
/ ``pass→inconclusive`` collapses the aggregate gate misses. Pure functions, so no
container / DB is needed. See ``deploy_service`` and CLAUDE.md (Code domain).
"""
from backend.services.code import deploy_service as ds


# --- _source_changed_meaningfully (no-op skip) -------------------------------
def test_source_change_identical_is_noop():
    base = {"main.go": b"package main", "go.mod": b"module x"}
    assert ds._source_changed_meaningfully(dict(base), base) is False


def test_source_change_lockfile_only_is_noop():
    base = {"main.go": b"package main", "go.sum": b"old-sums"}
    cand = {"main.go": b"package main", "go.sum": b"new-sums-from-go-build"}
    # Only go.sum churned (native build verification) -> treated as no-op.
    assert ds._source_changed_meaningfully(cand, base) is False


def test_source_change_real_edit_is_meaningful():
    base = {"main.go": b"package main", "package-lock.json": b"{}"}
    cand = {"main.go": b"package main // fixed", "package-lock.json": b"{churned}"}
    assert ds._source_changed_meaningfully(cand, base) is True


def test_source_change_new_or_removed_file_is_meaningful():
    base = {"a.py": b"x"}
    assert ds._source_changed_meaningfully({"a.py": b"x", "b.py": b"y"}, base) is True
    assert ds._source_changed_meaningfully({}, base) is True


# --- _itest_critical_fail_endpoints ------------------------------------------
def test_critical_fail_endpoints_from_cases_and_summary():
    it = {
        "cases": [
            {"endpoint": "GET /a", "deterministic_fail": True, "severity": "critical"},
            {"endpoint": "GET /b", "deterministic_fail": True, "severity": "warning"},
            {"endpoint": "GET /c", "deterministic_fail": False, "severity": "critical"},
        ],
        "summary": {"failed": [{"endpoint": "POST /login"}]},
    }
    eps = ds._itest_critical_fail_endpoints(it)
    assert eps == {"GET /a", "POST /login"}  # /b is warning, /c didn't fail


# --- _repair_regressed: HARD checks apply in BOTH modes ----------------------
def _smoke(ok_eps=(), fivexx_eps=(), inconclusive_eps=()):
    out = [{"endpoint": e, "result": "ok"} for e in ok_eps]
    out += [{"endpoint": e, "result": "5xx"} for e in fivexx_eps]
    out += [{"endpoint": e, "result": "inconclusive (Timeout)"} for e in inconclusive_eps]
    return out


def test_regress_smoke_ok_to_5xx_both_modes():
    base = _smoke(ok_eps=["GET /x"])
    cand = _smoke(fivexx_eps=["GET /x"])
    for has_defect in (True, False):
        reg, why = ds._repair_regressed(has_defect=has_defect, start_smoke=base, cand_smoke=cand,
                                        start_itest={"gate": "pass"}, cand_itest={"gate": "pass"})
        assert reg is True and "5xx" in why


def test_regress_new_critical_failing_endpoint_both_modes():
    start_it = {"gate": "fail", "cases": [{"endpoint": "GET /a", "deterministic_fail": True, "severity": "critical"}]}
    cand_it = {"gate": "fail", "cases": [
        {"endpoint": "GET /a", "deterministic_fail": True, "severity": "critical"},
        {"endpoint": "GET /b", "deterministic_fail": True, "severity": "critical"},  # NEW broken endpoint
    ]}
    reg, why = ds._repair_regressed(has_defect=True, start_smoke=[], cand_smoke=[],
                                    start_itest=start_it, cand_itest=cand_it)
    assert reg is True and "/b" in why


# --- _repair_regressed: SOFT checks only on a healthy (proactive) baseline ----
def test_proactive_regress_smoke_ok_to_inconclusive():
    base = _smoke(ok_eps=["GET /x"])
    cand = _smoke(inconclusive_eps=["GET /x"])  # endpoint now hangs / resets
    reg, _ = ds._repair_regressed(has_defect=False, start_smoke=base, cand_smoke=cand,
                                  start_itest={"gate": "pass"}, cand_itest={"gate": "pass"})
    assert reg is True


def test_defect_round_tolerates_smoke_ok_to_inconclusive():
    # In a defect round a slow/warming endpoint must NOT block a real fix.
    base = _smoke(ok_eps=["GET /x"])
    cand = _smoke(inconclusive_eps=["GET /x"])
    reg, _ = ds._repair_regressed(has_defect=True, start_smoke=base, cand_smoke=cand,
                                  start_itest={"gate": "fail"}, cand_itest={"gate": "fail"})
    assert reg is False


def test_proactive_regress_itest_pass_to_inconclusive():
    reg, why = ds._repair_regressed(
        has_defect=False, start_smoke=[], cand_smoke=[],
        start_itest={"gate": "pass", "summary": {"executed": 8, "token": True}},
        cand_itest={"gate": "inconclusive", "summary": {"executed": 0, "token": False}})
    assert reg is True


def test_proactive_regress_token_lost():
    reg, _ = ds._repair_regressed(
        has_defect=False, start_smoke=[], cand_smoke=[],
        start_itest={"gate": "pass", "summary": {"executed": 5, "token": True}},
        cand_itest={"gate": "pass", "summary": {"executed": 5, "token": False}})
    assert reg is True


def test_proactive_regress_coverage_shrank():
    reg, _ = ds._repair_regressed(
        has_defect=False, start_smoke=[], cand_smoke=[],
        start_itest={"gate": "pass", "summary": {"executed": 10, "token": True}},
        cand_itest={"gate": "pass", "summary": {"executed": 6, "token": True}})
    assert reg is True


def test_no_regression_when_stable():
    base = _smoke(ok_eps=["GET /x"])
    reg, why = ds._repair_regressed(
        has_defect=False, start_smoke=base, cand_smoke=_smoke(ok_eps=["GET /x"]),
        start_itest={"gate": "pass", "summary": {"executed": 8, "token": True}},
        cand_itest={"gate": "pass", "summary": {"executed": 8, "token": True}})
    assert reg is False and why == ""


def test_proactive_improvement_not_regression():
    # A proactive pass that turns inconclusive coverage INTO real passing coverage
    # (executed grows) is an improvement, not a regression.
    reg, _ = ds._repair_regressed(
        has_defect=False, start_smoke=[], cand_smoke=[],
        start_itest={"gate": "inconclusive", "summary": {"executed": 0, "token": False}},
        cand_itest={"gate": "pass", "summary": {"executed": 9, "token": True}})
    assert reg is False
