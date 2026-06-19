"""
Regression tests for the Code service JSON parsers.

The bug: model output is a bare JSON array/object, but its string values can
contain Markdown ``` code fences (code samples in the generated docs). The old
parser ran a ``` search unconditionally and matched an *inner* fence, extracting
garbage and failing to parse a perfectly valid array.
"""
import json

from backend.services.code.generation_service import CodeGenerationService

svc = CodeGenerationService()


def test_array_with_inner_code_fence_parses():
    """A bare array whose content contains ``` fences must still parse."""
    content = "用法示例：\n```json\n{\"a\": 1}\n```\n说明结束。"
    payload = [
        {"document_type": "frontend_spec", "title": "前端", "content": content,
         "prompt_expert": "见 ```python\nprint(1)\n``` 示例", "order_index": 0},
        {"document_type": "backend_spec", "title": "后端", "content": "无代码", "order_index": 1},
    ]
    raw = json.dumps(payload, ensure_ascii=False)
    parsed = svc._parse_json_array(raw)
    assert len(parsed) == 2
    assert parsed[0]["document_type"] == "frontend_spec"
    assert "```json" in parsed[0]["content"]  # inner fence preserved, not stripped


def test_bare_object_salvaged_with_inner_code_fence():
    """A single bare {...} object (no array framing) is salvaged into a
    one-element list, with inner ``` fences in its values left intact."""
    obj = {"content": "代码：\n```ts\nconst x = 1;\n```", "prompt_expert": "改进建议"}
    raw = json.dumps(obj, ensure_ascii=False)
    parsed = svc._parse_json_array(raw)
    assert len(parsed) == 1
    assert parsed[0]["content"].startswith("代码")
    assert "```ts" in parsed[0]["content"]  # inner fence preserved, not stripped


def test_outer_fenced_array_still_parses():
    """An array genuinely wrapped in a ```json fence must still unwrap."""
    raw = "```json\n[{\"document_type\": \"x\", \"title\": \"t\", \"content\": \"c\"}]\n```"
    parsed = svc._parse_json_array(raw)
    assert len(parsed) == 1 and parsed[0]["document_type"] == "x"


def test_array_with_surrounding_prose_parses():
    raw = "好的，这是结果：\n[{\"document_type\": \"x\", \"title\": \"t\", \"content\": \"c\"}]\n以上。"
    parsed = svc._parse_json_array(raw)
    assert len(parsed) == 1


def test_raw_control_chars_tolerated():
    """Real newlines inside string values (strict JSON-illegal) must be tolerated."""
    raw = '[{"document_type": "x", "title": "t", "content": "line1\nline2"}]'
    parsed = svc._parse_json_array(raw)
    assert len(parsed) == 1
    assert "line1" in parsed[0]["content"]


def test_unparseable_returns_empty():
    assert svc._parse_json_array("not json at all") == []


def test_strip_code_fence_only_unwraps_outer():
    # bare array unchanged
    assert CodeGenerationService._strip_code_fence("[1,2]") == "[1,2]"
    # genuine wrapper unwrapped
    assert CodeGenerationService._strip_code_fence("```json\n[1,2]\n```") == "[1,2]"
    # inner fence in a bare object is NOT touched
    text = '{"c": "```py\\ncode\\n```"}'
    assert CodeGenerationService._strip_code_fence(text) == text
