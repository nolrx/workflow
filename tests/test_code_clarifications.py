"""
Unit tests for the requirements clarification questionnaire normalizer.

The model emits a JSON array of clarification questions (see
docs/requirements-clarify-spec.md); ``_normalize_clarifications`` must be
tolerant — coercing types, repairing/clamping defaults, dropping unusable
questions, and capping counts — so a sloppy model payload never breaks the
front-end confirmation dialog. No network / AI calls here.
"""
import json

from backend.services.code.generation_service import CodeGenerationService

svc = CodeGenerationService()


def _one(question: dict) -> dict | None:
    return svc._normalize_clarification(question, 0)


def test_single_keeps_given_default_and_defaults_allow_custom():
    out = _one(
        {
            "id": "platform",
            "category": "平台与范围",
            "question": "平台?",
            "type": "single",
            "options": [{"value": "web", "label": "Web"}, {"value": "app", "label": "App"}],
            "default": ["web"],
        }
    )
    assert out["type"] == "single"
    assert out["default"] == ["web"]
    assert out["allow_custom"] is True
    assert out["category"] == "平台与范围"


def test_single_without_default_falls_back_to_first_option():
    out = _one(
        {
            "type": "single",
            "question": "P?",
            "options": [{"value": "a", "label": "A"}, {"value": "b", "label": "B"}],
        }
    )
    assert out["default"] == ["a"]
    assert out["id"] == "q1"  # missing id -> index-derived fallback


def test_single_default_trimmed_to_one():
    out = _one(
        {
            "type": "single",
            "question": "P?",
            "options": [{"value": "a", "label": "A"}, {"value": "b", "label": "B"}],
            "default": ["a", "b"],
        }
    )
    assert out["default"] == ["a"]


def test_multi_aliases_and_default_matched_by_label_deduped():
    out = _one(
        {
            "type": "checkbox",  # alias -> multi
            "question": "用户?",
            "options": [{"value": "ind", "label": "个人"}, {"value": "team", "label": "团队"}],
            "default": ["个人", "个人", "团队"],  # by label, with a duplicate
        }
    )
    assert out["type"] == "multi"
    assert out["default"] == ["ind", "team"]


def test_multi_default_with_unknown_value_becomes_empty():
    out = _one(
        {
            "type": "multi",
            "question": "P?",
            "options": [{"value": "a", "label": "A"}, {"value": "b", "label": "B"}],
            "default": ["does-not-exist"],
        }
    )
    assert out["default"] == []


def test_string_options_are_normalized():
    out = _one({"question": "P?", "options": ["选项一", "选项二"], "default": ["选项一"]})
    assert out["options"][0] == {"value": "选项一", "label": "选项一"}
    assert out["default"] == ["选项一"]


def test_question_with_fewer_than_two_options_is_dropped():
    assert _one({"question": "x", "options": [{"value": "only", "label": "Only"}]}) is None


def test_options_as_string_is_dropped_not_split_into_characters():
    # A model emitting options as a string (not a list) must not be iterated
    # character-by-character into bogus single-letter options; coerced to [] so
    # the question drops out via the <2-options rule.
    assert _one({"question": "P?", "options": "web,mobile", "default": ["web"]}) is None


def test_options_as_scalar_does_not_raise():
    assert _one({"question": "P?", "options": 5}) is None


def test_empty_question_text_is_dropped():
    assert _one(
        {"question": "  ", "options": [{"value": "a", "label": "A"}, {"value": "b", "label": "B"}]}
    ) is None


def test_id_is_sanitized():
    out = _one(
        {
            "id": "my id!/x",
            "type": "single",
            "question": "P?",
            "options": [{"value": "a", "label": "A"}, {"value": "b", "label": "B"}],
        }
    )
    assert out["id"] == "my_id__x"


def test_options_capped_at_five():
    out = _one(
        {
            "question": "P?",
            "options": [{"value": f"v{i}", "label": f"L{i}"} for i in range(9)],
        }
    )
    assert len(out["options"]) == 5


def test_normalize_list_caps_questions_and_drops_bad_ones():
    items = [
        {"question": f"Q{i}?", "options": [{"value": "a", "label": "A"}, {"value": "b", "label": "B"}]}
        for i in range(9)
    ]
    items.append({"question": "bad", "options": []})  # unusable, must be dropped
    raw = json.dumps(items, ensure_ascii=False)
    questions = svc._normalize_clarifications(raw)
    assert len(questions) == 6  # capped


def test_empty_or_unparseable_payload_yields_empty_list():
    assert svc._normalize_clarifications("[]") == []
    assert svc._normalize_clarifications("not json at all") == []


def test_fallback_clarifications_are_self_consistent():
    """The local fallback questionnaire must itself satisfy the spec/normalizer."""
    fallback = svc._fallback_clarifications()
    raw = json.dumps(fallback, ensure_ascii=False)
    normalized = svc._normalize_clarifications(raw)
    assert len(normalized) == len(fallback)
    for question in normalized:
        assert question["type"] in ("single", "multi")
        assert len(question["options"]) >= 2
        values = {opt["value"] for opt in question["options"]}
        assert all(value in values for value in question["default"])
        if question["type"] == "single":
            assert len(question["default"]) == 1
