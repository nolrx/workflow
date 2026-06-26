"""
Unit tests for the typed node-contract layer (no Flask app / DB needed).

Covers the port-type registry, NodeContract identity/loading, the built-in stage
contracts, and ``validate_graph`` over a real ``CanvasGraph`` — including that
typed and freeform nodes coexist.
"""
import pytest

from backend.services.agent.contracts import (
    NodeContract,
    Port,
    is_registered,
    load_contract,
    register_port_type,
    validate_contract_resources,
    validate_graph,
)
from backend.services.agent.contracts.defaults import (
    get_default_contract,
    iter_default_node_contracts,
)
from backend.services.agent.dag_engine import CanvasGraph


# --- graph construction helpers ------------------------------------------------
def _stage(nid: str, contract_key: str, label: str | None = None) -> dict:
    return {
        "id": nid,
        "type": "stage",
        "data": {"label": label or nid, "config": {"contract_key": contract_key}},
    }


def _freeform(nid: str, node_type: str = "agent") -> dict:
    return {"id": nid, "type": node_type, "data": {"label": nid, "config": {}}}


def _edge(src: str, tgt: str, source_handle: str, target_handle: str, order: int = 0) -> dict:
    return {
        "id": f"{src}->{tgt}",
        "source": src,
        "target": tgt,
        "sourceHandle": source_handle,
        "targetHandle": target_handle,
        "data": {"order": order},
    }


def _graph(nodes, edges) -> CanvasGraph:
    return CanvasGraph(nodes, edges)


# --- port registry -------------------------------------------------------------
def test_builtin_port_types_registered():
    for key in (
        "core:context_ledger",
        "core:user_text",
        "code:requirements_doc",
        "code:development_flow",
        "code:ui_preview",
        "code:deployment",
    ):
        assert is_registered(key)
    assert not is_registered("code:does_not_exist")


def test_register_rejects_unnamespaced_key():
    with pytest.raises(ValueError):
        register_port_type("nonamespace", ref_kinds={"inline_text"}, describe="x")


def test_register_rejects_unknown_ref_kind():
    with pytest.raises(ValueError):
        register_port_type("test:bad", ref_kinds={"made_up_kind"}, describe="x")


# --- contract identity / loading ----------------------------------------------
def test_default_contracts_present_and_shaped():
    types = {c.node_type for c in iter_default_node_contracts()}
    assert {"requirements", "flow", "documents", "style", "preview", "deploy"} <= types

    req = get_default_contract("requirements")
    assert req.output_type("doc") == "code:requirements_doc"
    assert req.input("brief").required is True
    assert req.review_gate is True

    flow = get_default_contract("flow")
    assert flow.input("requirements").type == "code:requirements_doc"


def test_spec_hash_is_stable_and_sensitive():
    a = get_default_contract("requirements")
    b = load_contract(a.to_dict())
    assert a.spec_hash() == b.spec_hash()  # round-trip preserves identity

    mutated = NodeContract(
        node_type=a.node_type,
        role=a.role,
        inputs=a.inputs,
        outputs=[Port("doc", "code:development_flow")],  # changed output type
        prompt_ref=a.prompt_ref,
        pricing_key=a.pricing_key,
        executor=a.executor,
    )
    assert mutated.spec_hash() != a.spec_hash()


def test_load_contract_tolerates_partial():
    c = load_contract({"node_type": "x"})
    assert c.node_type == "x"
    assert c.inputs == [] and c.outputs == []
    assert c.prompt_ref is None


# --- validate_graph ------------------------------------------------------------
def test_valid_chain_has_no_errors():
    nodes = [
        _freeform("src", "source_doc"),
        _stage("R", "requirements"),
        _stage("F", "flow"),
    ]
    edges = [
        _edge("src", "R", "out", "brief"),       # untyped -> R.brief (satisfies required)
        _edge("R", "F", "doc", "requirements"),  # typed, matching types
    ]
    assert validate_graph(_graph(nodes, edges), get_default_contract) == []


def test_type_mismatch_is_reported():
    nodes = [_stage("R", "requirements"), _stage("D", "documents")]
    # R.doc is code:requirements_doc; D.flow expects code:development_flow.
    edges = [_edge("R", "D", "doc", "flow")]
    errors = validate_graph(_graph(nodes, edges), get_default_contract)
    assert any("类型不匹配" in e for e in errors)


def test_missing_required_input_is_reported():
    nodes = [_stage("F", "flow")]  # flow.requirements required, nothing wired
    errors = validate_graph(_graph(nodes, []), get_default_contract)
    assert any("必填输入未连接" in e and "requirements" in e for e in errors)


def test_unknown_contract_is_reported():
    nodes = [_stage("X", "no_such_contract")]
    errors = validate_graph(_graph(nodes, []), get_default_contract)
    assert any("未知契约" in e for e in errors)


def test_cycle_is_reported():
    nodes = [_stage("A", "requirements"), _stage("B", "flow")]
    edges = [_edge("A", "B", "doc", "requirements"), _edge("B", "A", "doc", "brief")]
    errors = validate_graph(_graph(nodes, edges), get_default_contract)
    assert any("环" in e for e in errors)


def test_freeform_nodes_are_not_type_checked():
    # An agent (freeform) node feeding a typed node must not raise a type error.
    nodes = [_freeform("A", "agent"), _stage("R", "requirements")]
    edges = [_edge("A", "R", "anything", "brief")]
    assert validate_graph(_graph(nodes, edges), get_default_contract) == []


# --- resource existence --------------------------------------------------------
def test_builtin_contracts_reference_real_resources():
    for contract in iter_default_node_contracts():
        assert validate_contract_resources(contract) == [], contract.node_type


def test_to_catalog_shape():
    cat = get_default_contract("requirements").to_catalog()
    assert cat["node_type"] == "requirements"
    assert cat["review_gate"] is True
    assert cat["executable"] is True  # stage_text is wired
    assert {"name": "doc", "type": "code:requirements_doc"} in cat["outputs"]
    # preview (stage_preview) and deploy are wired too
    assert get_default_contract("preview").to_catalog()["executable"] is True
    # all stage executors are wired now
    for nt in ("deploy", "be_build", "fe_build", "mw_provision"):
        assert get_default_contract(nt).to_catalog()["executable"] is True


def test_deploy_contract_is_backend_centric():
    dep = get_default_contract("deploy")
    assert dep.input("backend").required is True
    assert dep.input("middleware").required is False
    # frontend is served statically at /preview, NOT a deploy input
    assert dep.input("frontend") is None


def test_deploy_accepts_existing_backend_source():
    """'I already have a frontend, just deploy the backend' → existing_backend → deploy."""
    nodes = [_freeform("src", "source_doc"), _stage("D", "deploy")]
    edges = [_edge("src", "D", "out", "backend")]  # untyped source feeds deploy.backend
    assert validate_graph(_graph(nodes, edges), get_default_contract) == []


def test_deploy_without_backend_is_rejected():
    errors = validate_graph(_graph([_stage("D", "deploy")], []), get_default_contract)
    assert any("必填输入未连接" in e and "backend" in e for e in errors)


def test_existing_source_emits_typed_port_value():
    from backend.services.agent.workflows.code_canvas_workflow import (
        _EXISTING_SOURCE_SPEC,
        _source_port_value,
    )

    assert set(_EXISTING_SOURCE_SPEC) == {
        "existing_frontend",
        "existing_backend",
        "existing_contract",
        "existing_middleware",
    }
    # existing_contract has no source workflow → no DB lookup, pure type marker.
    pv = _source_port_value(None, {"source_kind": "existing_contract"}, "n1")
    assert pv["type"] == "code:api_contract"
    assert pv["ref_kind"] == "code_ledger_field"
    # a plain text source is not an existing-product source
    assert _source_port_value(None, {"source_kind": "requirements_doc"}, "n1") is None


def test_freeze_stamps_only_prompted_stage_nodes():
    from backend.services.agent.contracts import freeze_stage_prompts

    def fake_pin(key):
        return {"key": key, "version": 3, "hash": "sha256:frozen"}

    nodes = [
        {"id": "s1", "type": "source_doc", "data": {"config": {"source_kind": "requirements_doc"}}},
        {"id": "R", "type": "stage", "data": {"config": {"contract_key": "requirements"}}},
        {"id": "P", "type": "stage", "data": {"config": {"contract_key": "preview"}}},  # no prompt
    ]
    out, pinned = freeze_stage_prompts(nodes, get_default_contract, fake_pin)
    by_id = {n["id"]: n for n in out}
    assert pinned == 1  # only requirements has a prompt
    assert by_id["R"]["data"]["config"]["prompt_pin"]["hash"] == "sha256:frozen"
    assert "prompt_pin" not in by_id["P"]["data"]["config"]  # preview has no prompt_ref
    assert "prompt_pin" not in by_id["s1"]["data"]["config"]  # freeform untouched
