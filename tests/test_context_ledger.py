"""Unit tests for the context ledger's FR/NFR requirement registry (anti-drift).

The registry is the cross-stage traceability anchor: requirements seeds the
canonical FR/NFR ids, and every downstream prompt renders them so modules /
documents / builds reference (not re-describe) requirements.
"""
from backend.services.agent.context_ledger import ContextLedger
from backend.services.agent.workflows.code_workflow import _req_items_from_section


def test_req_items_parses_fr_nfr_and_skips_plain_lines():
    body = (
        "- FR1: 用户登录\n"
        "- FR2 注册账户\n"
        "- 普通说明行，无编号\n"
        "- **NFR1**：首屏 < 2s\n"
        "- NFR2) 离线可用"
    )
    items = _req_items_from_section(body)
    assert [i["id"] for i in items] == ["FR1", "FR2", "NFR1", "NFR2"]
    assert items[0] == {"id": "FR1", "kind": "FR", "statement": "用户登录"}
    assert items[3]["kind"] == "NFR"


def test_ledger_registers_and_renders_requirements():
    led = ContextLedger.empty()
    led.merge(
        project={"one_liner": "待办应用"},
        requirements_add=[
            {"id": "FR1", "kind": "FR", "statement": "登录"},
            {"id": "NFR1", "kind": "NFR", "statement": "首屏快"},
        ],
    )
    rendered = led.render_for_prompt()
    assert "需求条目登记" in rendered
    assert "[FR1] 登录" in rendered
    assert "[NFR1] 首屏快" in rendered
    assert led.fingerprint()["requirements"] == {"FR1": "登录", "NFR1": "首屏快"}


def test_requirements_merge_is_idempotent_by_id():
    led = ContextLedger.empty()
    led.merge(requirements_add=[{"id": "FR1", "statement": "first"}])
    led.merge(
        requirements_add=[
            {"id": "FR1", "statement": "updated"},  # same id -> update in place
            {"id": "FR2", "statement": "two"},      # new id -> appended
        ]
    )
    reqs = {r["id"]: r["statement"] for r in led.to_dict()["requirements"]}
    assert reqs == {"FR1": "updated", "FR2": "two"}


def test_legacy_ledger_without_requirements_loads():
    led = ContextLedger.load({"schema_version": 1, "project": {"one_liner": "x"}})
    assert led.to_dict()["requirements"] == []
    assert isinstance(led.render_for_prompt(), str)
