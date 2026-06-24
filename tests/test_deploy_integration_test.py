"""
Unit tests for the deploy-time frontend↔backend integration test gate
(``backend.services.code.fullstack.integration_test_service``).

These exercise the PURE / deterministic logic only — payload classification, the
case judge (which observed responses roll a deploy back vs warn), plan
sanitisation, the contract-only fallback plan, and the frontend API digest. No
network, DB, Flask app, or AI provider is touched (the module lazy-imports all of
those inside functions), so they run under ``pytest -m "not integration"``.
"""
from backend.services.code.fullstack import integration_test_service as its


# --- payload field reachability ----------------------------------------------
def test_field_missing_bare_list():
    assert its._field_missing([{"id": 1, "title": "x"}], "title") is False
    assert its._field_missing([{"id": 1}], "title") is True


def test_field_missing_envelope():
    body = {"data": [{"id": 1, "name": "a"}], "total": 1}
    assert its._field_missing(body, "name") is False
    assert its._field_missing(body, "missing") is True


def test_field_missing_nested_envelope():
    body = {"data": {"items": [{"id": 1, "title": "t"}]}}
    assert its._field_missing(body, "title") is False
    assert its._field_missing(body, "nope") is True


def test_field_missing_empty_collection_is_skip_not_fail():
    # No reachable record → cannot judge → must NOT report missing (no false rollback).
    assert its._field_missing({"data": []}, "id") is False
    assert its._field_missing([], "id") is False
    assert its._field_missing(5, "id") is False


def test_field_missing_plain_object():
    assert its._field_missing({"id": 1, "name": "a"}, "name") is False
    assert its._field_missing({"id": 1}, "name") is True


# --- empty-list classification (advisory) ------------------------------------
def test_is_empty_list():
    assert its._is_empty_list([]) is True
    assert its._is_empty_list([1]) is False
    assert its._is_empty_list({"data": []}) is True
    assert its._is_empty_list({"data": [1]}) is False
    assert its._is_empty_list({"items": [], "total": 3}) is False  # total>0 → not empty
    assert its._is_empty_list({"id": 1}) is False  # not a collection


# --- the deterministic judge -------------------------------------------------
def _case(**expect):
    return {"severity": "critical", "expect": expect}


def test_judge_no_response_is_inconclusive():
    v = its._judge_case(_case(json=True), status=None, parsed_ok=False, body=None)
    assert v["outcome"] == "inconclusive" and v["deterministic_fail"] is False


def test_judge_5xx_is_deterministic_fail():
    for code in (500, 502, 503):
        v = its._judge_case(_case(), status=code, parsed_ok=False, body=None, has_body=True)
        assert v["outcome"] == "fail" and v["deterministic_fail"] is True


def test_judge_unparseable_json_fails_only_when_server_declares_json():
    # Server declares JSON but the body won't parse → definitive break → det fail.
    v = its._judge_case(_case(json=True), status=200, parsed_ok=False, body=None,
                        has_body=True, content_type="application/json; charset=utf-8")
    assert v["outcome"] == "fail" and v["deterministic_fail"] is True
    # JSON merely EXPECTED but the server returns a non-JSON content type (a text
    # /health, a file/CSV download, a cold-start HTML page) → WARN, never a rollback.
    v2 = its._judge_case(_case(json=True), status=200, parsed_ok=False, body=b"ok",
                         has_body=True, content_type="text/plain")
    assert v2["outcome"] == "warn" and v2["deterministic_fail"] is False
    # Empty body (e.g. 204 / empty 200) must NOT be flagged as a parse failure.
    v3 = its._judge_case(_case(json=True), status=204, parsed_ok=False, body=None,
                         has_body=False, content_type="application/json")
    assert v3["deterministic_fail"] is False


def test_judge_3xx_with_body_never_rolls_back():
    # A terminal 3xx with a non-JSON body must not be judged a JSON-parse failure
    # (the parse gate is `status < 300`, aligned with the required-field branch).
    for code in (301, 302, 307):
        v = its._judge_case(_case(json=True), status=code, parsed_ok=False, body=b"<html>",
                            has_body=True, content_type="text/html")
        assert v["deterministic_fail"] is False


def test_judge_required_field_missing_on_2xx_is_fail():
    v = its._judge_case(_case(json=True, required_fields=["name"]),
                        status=200, parsed_ok=True, body={"id": 1})
    assert v["outcome"] == "fail" and v["deterministic_fail"] is True


def test_judge_required_fields_present_is_pass():
    v = its._judge_case(_case(json=True, required_fields=["id", "name"]),
                        status=200, parsed_ok=True, body={"id": 1, "name": "a"})
    assert v["outcome"] == "pass" and v["deterministic_fail"] is False


def test_judge_4xx_is_warn_never_rollback():
    for code in (400, 401, 403, 404, 422):
        v = its._judge_case(_case(json=True), status=code, parsed_ok=False, body=None, has_body=True)
        assert v["outcome"] == "warn" and v["deterministic_fail"] is False


def test_judge_empty_list_is_warn_not_fail():
    v = its._judge_case(_case(json=True, nonempty_list=True),
                        status=200, parsed_ok=True, body={"data": []})
    assert v["outcome"] == "warn" and v["deterministic_fail"] is False


# --- plan sanitisation -------------------------------------------------------
def test_sanitize_plan_normalises_and_filters():
    raw = [
        "not a dict",
        {"path": "no-leading-slash"},
        {"method": "get", "path": "/orders", "severity": "weird",
         "expect": {"required_fields": ["id", 5, "title"]}},
        {"method": "post", "path": "/orders", "body": {"x": 1}, "auth": True},
    ]
    plan = its._sanitize_plan(raw)
    assert len(plan) == 2
    g = plan[0]
    assert g["method"] == "GET" and g["path"] == "/orders"
    assert g["severity"] == "critical"  # unknown severity → default critical
    assert g["expect"]["required_fields"] == ["id", "title"]  # non-str dropped
    p = plan[1]
    assert p["method"] == "POST" and p["auth"] is True and p["body"] == {"x": 1}


def test_sanitize_plan_caps_at_max():
    raw = [{"method": "GET", "path": f"/p{i}"} for i in range(its.ITEST_MAX_TESTS + 10)]
    assert len(its._sanitize_plan(raw)) == its.ITEST_MAX_TESTS


# --- contract-only fallback plan ---------------------------------------------
def test_contract_fallback_plan():
    contract = {"openapi": {"paths": {
        "/health": {"get": {}},
        "/orders": {"get": {}},
        "/orders/{id}": {"get": {}},   # path param → skipped
        "/login": {"post": {}},        # no get → skipped, but marks has_login
    }}}
    plan = its._contract_fallback_plan(contract)
    paths = {c["path"]: c for c in plan}
    assert set(paths) == {"/health", "/orders"}
    assert paths["/health"]["auth"] is False        # health is public
    assert paths["/orders"]["auth"] is True          # has_login → owner-scoped GET sends auth
    assert paths["/orders"]["expect"]["required_fields"] == []  # fallback omits field checks
    assert paths["/orders"]["method"] == "GET"


def test_contract_fallback_plan_no_login():
    contract = {"openapi": {"paths": {"/items": {"get": {}}}}}
    plan = its._contract_fallback_plan(contract)
    assert plan[0]["auth"] is False  # no login endpoint → don't attempt auth


def test_contract_fallback_plan_empty_contract():
    assert its._contract_fallback_plan({}) == []
    assert its._contract_fallback_plan({"openapi": {}}) == []


# --- frontend API digest -----------------------------------------------------
def test_frontend_api_digest_picks_api_files():
    files = {
        "src/api/client.ts": b"const base = window.__API_BASE__; fetch(`${base}/orders`)",
        "src/util.ts": b"export const x = 1",          # no API hints → excluded
        "README.md": b"# hi /orders",                   # not a source ext → excluded
        "src/types.ts": b"export interface Order { id: string; title: string }",
    }
    digest = its._frontend_api_digest(files)
    assert "src/api/client.ts" in digest
    assert "/orders" in digest
    assert "src/types.ts" in digest
    assert "src/util.ts" not in digest
    assert "README.md" not in digest


def test_frontend_api_digest_empty():
    assert its._frontend_api_digest({}) == ""


# --- misc helpers ------------------------------------------------------------
def test_is_critical_defaults_critical():
    assert its._is_critical({"severity": "critical"}) is True
    assert its._is_critical({"severity": "warning"}) is False
    assert its._is_critical({}) is True  # default strict


def test_extract_json_tolerant():
    assert its._extract_json('```json\n{"tests": []}\n```') == {"tests": []}
    assert its._extract_json('prefix {"a": 1} suffix') == {"a": 1}
    assert its._extract_json("not json at all") is None
    assert its._extract_json("") is None


# --- repair digest (feeds the contract-repair agent) -------------------------
def test_format_failures_for_repair_only_includes_deterministic_failures():
    itest = {"cases": [
        {"endpoint": "GET /orders", "severity": "critical", "deterministic_fail": True,
         "reason": "HTTP 500(后端 5xx)", "frontend_reads": "OrderList 渲染 .title"},
        {"endpoint": "GET /users", "severity": "critical", "deterministic_fail": True,
         "reason": "HTTP 200 但缺少前端解析所需字段 ['email']", "frontend_reads": ""},
        {"endpoint": "GET /ok", "deterministic_fail": False, "reason": "HTTP 200"},
    ]}
    digest = its.format_failures_for_repair(itest, contract_block="CONTRACT-BLOCK-X",
                                            logs="Traceback (most recent call last)")
    assert "GET /orders" in digest and "5xx" in digest
    assert "OrderList 渲染 .title" in digest        # frontend_reads surfaced
    assert "GET /users" in digest and "email" in digest
    assert "GET /ok" not in digest                  # passing case excluded
    assert "Traceback" in digest                    # container logs included
    assert "CONTRACT-BLOCK-X" in digest             # contract included


def test_format_failures_for_repair_empty():
    digest = its.format_failures_for_repair({"cases": []})
    assert "无逐条用例明细" in digest
