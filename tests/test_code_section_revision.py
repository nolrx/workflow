"""
Unit tests for inline section (partial) revision in the Code generation service.

``revise_section`` rewrites only a user-selected span of a confirmed document: it
feeds the whole document plus the selected substring to the model and returns the
full revised text. These tests mock the text provider (no network) and verify
prompt assembly, the streamed result, the best-effort fallback, and kind routing.
"""
import pytest

import backend.services.code.generation_service as gen_mod
from backend.services.code.generation_service import CodeGenerationService

svc = CodeGenerationService()


class _FakeProvider:
    provider_name = "fake"
    model = "fake-model"

    def __init__(self, chunks=None, configured=True, capture=None):
        self._chunks = chunks if chunks is not None else ["ok"]
        self._configured = configured
        self._capture = capture

    def is_configured(self):
        return self._configured

    def generate_text_stream(self, prompt):
        if self._capture is not None:
            self._capture["prompt"] = prompt
        for chunk in self._chunks:
            yield chunk


def test_revise_section_returns_streamed_replacement(monkeypatch):
    # The model returns ONLY the replacement for the selected span (not the doc).
    fake = _FakeProvider(chunks=["重写后的", "片段"])
    monkeypatch.setattr(gen_mod, "get_text_provider", lambda *a, **k: fake)
    out = svc.revise_section(
        "requirements", current_doc="整篇文档...某段...结尾", selected_text="某段", instruction="改一下"
    )
    assert out == "重写后的片段"


def test_revise_section_falls_back_to_selected_text_when_unconfigured(monkeypatch):
    # A failed/unconfigured provider returns the original span -> a no-op splice
    # that never discards the user's confirmed work.
    fake = _FakeProvider(configured=False)
    monkeypatch.setattr(gen_mod, "get_text_provider", lambda *a, **k: fake)
    out = svc.revise_section("style", current_doc="STYLE DOC", selected_text="DOC", instruction="y")
    assert out == "DOC"


def test_revise_section_all_kinds_embed_selection_and_instruction(monkeypatch):
    capture: dict = {}
    monkeypatch.setattr(
        gen_mod, "get_text_provider", lambda *a, **k: _FakeProvider(capture=capture)
    )
    for kind in ("requirements", "flow", "style", "document"):
        capture.clear()
        out = svc.revise_section(
            kind, current_doc="DOCBODY", selected_text="SELSPAN", instruction="INSTR"
        )
        assert out == "ok"
        prompt = capture["prompt"]
        assert "SELSPAN" in prompt, kind
        assert "INSTR" in prompt, kind
        assert "DOCBODY" in prompt, kind


def test_revise_section_unknown_kind_raises():
    with pytest.raises(ValueError):
        svc.revise_section("nope", "doc", "sel", "instr")


# --- span resolution (offset-based splice target) ----------------------------

from backend.routes.code.project_routes import _resolve_span  # noqa: E402


def test_resolve_span_uses_exact_offsets_when_matching():
    doc = "0123一段文字89"
    s = doc.index("一段文字")
    assert _resolve_span(doc, s, s + 4, "一段文字") == (s, s + 4)


def test_resolve_span_falls_back_to_find_when_offsets_stale():
    # The doc shifted since selection, so the supplied offsets no longer match,
    # but the selected text still occurs verbatim -> locate it.
    doc = "prefix 改这里 suffix"
    idx = doc.index("改这里")
    assert _resolve_span(doc, 0, 2, "改这里") == (idx, idx + 3)


def test_resolve_span_none_when_text_absent():
    assert _resolve_span("nothing relevant here", 0, 3, "missing") is None


def test_resolve_span_handles_non_int_offsets():
    doc = "abc target xyz"
    idx = doc.index("target")
    assert _resolve_span(doc, None, None, "target") == (idx, idx + 6)
