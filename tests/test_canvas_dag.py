"""
Unit tests for the remix-canvas DAG engine and per-node text provider builder.

No network / DB: these exercise topological ordering, cycle detection, edge
ordering, and the factory's per-node ``build_text_provider`` resolution.
"""
import pytest

from backend.services.agent.dag_engine import CanvasGraph
from backend.services.ai.factory import build_text_provider


def _node(nid, ntype="agent"):
    return {"id": nid, "type": ntype, "data": {"label": nid, "config": {}}}


def _edge(src, tgt, order=0, handle=None):
    return {
        "id": f"{src}-{tgt}",
        "source": src,
        "target": tgt,
        "sourceHandle": handle,
        "data": {"order": order},
    }


def test_topo_order_linear():
    graph = CanvasGraph(
        [_node("a", "source_doc"), _node("b"), _node("c")],
        [_edge("a", "b"), _edge("b", "c")],
    )
    assert graph.topo_order() == ["a", "b", "c"]


def test_cycle_is_detected():
    graph = CanvasGraph([_node("a"), _node("b")], [_edge("a", "b"), _edge("b", "a")])
    with pytest.raises(ValueError):
        graph.topo_order()


def test_incoming_ordered_by_edge_order():
    graph = CanvasGraph(
        [_node("a", "source_doc"), _node("b", "source_doc"), _node("m", "merge")],
        [_edge("b", "m", order=1), _edge("a", "m", order=0)],
    )
    assert [e.source for e in graph.incoming("m")] == ["a", "b"]


def test_dangling_edge_is_ignored():
    graph = CanvasGraph([_node("a")], [_edge("a", "ghost")])
    assert graph.topo_order() == ["a"]
    assert graph.outgoing("a") == []


def test_branch_handle_preserved_on_edge():
    graph = CanvasGraph(
        [_node("b", "branch"), _node("x"), _node("y")],
        [_edge("b", "x", handle="yes"), _edge("b", "y", handle="no")],
    )
    out = {e.target: e.source_handle for e in graph.outgoing("b")}
    assert out == {"x": "yes", "y": "no"}


def test_build_text_provider_claude(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    provider = build_text_provider(provider="claude", model="claude-opus-4-8")
    assert provider is not None
    assert provider.provider_name == "claude"
    assert provider.model == "claude-opus-4-8"


def test_build_text_provider_image_provider_falls_back_to_gemini(monkeypatch):
    # openai/panlaxy are image-only — a text request must fall back to gemini.
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    provider = build_text_provider(provider="openai")
    assert provider is not None
    assert provider.provider_name == "gemini"


def test_build_text_provider_no_key_returns_none(monkeypatch):
    monkeypatch.setenv("AI_TEXT_PROVIDER", "claude")
    for key in (
        "AI_TEXT_API_KEY",
        "ANTHROPIC_API_KEY",
        "CLAUDE_API_KEY",
        "AI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    assert build_text_provider(provider="claude") is None
