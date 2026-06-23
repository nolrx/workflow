"""
Context verification — keeps each step's output on-口径 with the run's ledger.

Two layers, neither of which ever raises out to the workflow (a verification
failure must never break a run):

1. ``run_deterministic_checks`` — cheap, model-free structural checks (output
   non-empty, document-type coverage, required ledger fields present, frontend
   stack conformance). Failures are reported at ``warning`` level; the workflow
   continues.
2. ``run_ai_consistency_gate`` — a single lightweight model call at high-risk
   boundaries that compares a new product summary against the established ledger
   fingerprint and reports contradictions. Fails OPEN: an unconfigured provider
   returns ``None`` (gate skipped → program-only); any error returns a
   no-conflict verdict flagged ``degraded``.

``emit_context_events`` persists the per-step snapshot + check result and emits
the appropriate timeline event (CONTEXT_UPDATED / CONTEXT_CONFLICT).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from backend.models.agent import AgentEventLevel, AgentEventType
from backend.services.agent.context_ledger import ContextLedger
from backend.services.ai import get_text_provider
from backend.services.prompts import prompt_store

logger = logging.getLogger(__name__)

_AI_SUMMARY_CAP = 2000


# --- deterministic layer -----------------------------------------------------
def run_deterministic_checks(
    *,
    step_key: str,
    ledger: ContextLedger,
    new_output: dict,
    expectations: dict,
) -> dict:
    """Run the model-free checks selected by ``expectations``.

    ``new_output`` is a bag of named pieces the checks read from:
      - ``text``        : str — the produced document/text (nonempty_output)
      - ``doc_types``   : list[str] — produced document_type set (doc_types_covered)
      - ``frontend_summary`` : str — generated app summary (stack_conformance)
      - ``files``       : list[str] — produced file paths (stack_conformance)

    Returns ``{"ok", "level", "checks": [...], "summary"}``. A failing check sets
    ``level="warning"`` (never "error") so the main flow continues.
    """
    checks: list[dict] = []

    if expectations.get("nonempty_output"):
        min_chars = int(expectations.get("min_chars", 30))
        text = new_output.get("text") or ""
        ok = len(text.strip()) >= min_chars
        checks.append(
            {
                "name": "nonempty_output",
                "ok": ok,
                "detail": f"产物长度 {len(text.strip())} 字符（要求 ≥ {min_chars}）",
            }
        )

    required_types = expectations.get("doc_types_covered")
    if required_types:
        produced = {str(t) for t in (new_output.get("doc_types") or [])}
        missing = [t for t in required_types if t not in produced]
        checks.append(
            {
                "name": "doc_types_covered",
                "ok": not missing,
                "detail": (
                    "已覆盖全部基线文档类型"
                    if not missing
                    else f"缺少文档类型: {', '.join(missing)}"
                ),
            }
        )

    required_fields = expectations.get("required_ledger_fields")
    if required_fields:
        missing = [f for f in required_fields if not _ledger_field(ledger, f)]
        checks.append(
            {
                "name": "ledger_fields_complete",
                "ok": not missing,
                "detail": (
                    "账本关键字段齐备"
                    if not missing
                    else f"账本缺少字段: {', '.join(missing)}"
                ),
            }
        )

    stack = expectations.get("stack_conformance")
    if stack:
        summary = (new_output.get("frontend_summary") or "").lower()
        stack_text = (ledger.tech_stack.get("frontend") or "").lower()
        haystack = f"{summary} {stack_text}"
        missing_terms = [t for t in stack.get("must_include", []) if t.lower() not in haystack]
        must_file = stack.get("must_have_file")
        has_file = (not must_file) or (must_file in (new_output.get("files") or []))
        ok = not missing_terms and has_file
        detail = "前端产物与技术栈口径一致"
        if missing_terms:
            detail = f"未体现技术栈关键词: {', '.join(missing_terms)}"
        elif not has_file:
            detail = f"缺少入口文件 {must_file}"
        checks.append({"name": "stack_conformance", "ok": ok, "detail": detail})

    ok = all(c["ok"] for c in checks)
    failed = [c["name"] for c in checks if not c["ok"]]
    return {
        "ok": ok,
        "level": AgentEventLevel.INFO if ok else AgentEventLevel.WARNING,
        "checks": checks,
        "summary": "确定性校验通过" if ok else f"确定性校验告警: {', '.join(failed)}",
    }


def _ledger_field(ledger: ContextLedger, dotted: str) -> Any:
    """Read a dotted path like ``project.one_liner`` from the ledger; '' if absent."""
    node: Any = ledger.to_dict()
    for part in dotted.split("."):
        if isinstance(node, dict):
            node = node.get(part)
        else:
            return ""
    return node


# --- AI consistency gate -----------------------------------------------------
def gate_available() -> bool:
    """True when a text provider is configured, so a gate call would do real work.

    Lets the caller avoid *charging* for a gate that would otherwise be skipped
    (an unconfigured provider makes :func:`run_ai_consistency_gate` return None).
    """
    try:
        provider = get_text_provider()
    except Exception:  # noqa: BLE001
        return False
    return bool(provider and provider.is_configured())


def run_ai_consistency_gate(
    *,
    ledger: ContextLedger,
    new_product_summary: str,
    step_key: str,
    on_model_call=None,
) -> Optional[dict]:
    """Compare a new product summary against the established ledger口径.

    Returns ``None`` when no provider is configured (caller treats as "gate
    skipped — program-only"). Otherwise returns a verdict dict
    ``{"conflict": bool, "conflicts": [...], "summary": str, "degraded"?: bool}``.
    Never raises.
    """
    try:
        provider = get_text_provider()
    except Exception:  # noqa: BLE001 - factory must never break the run
        logger.warning("Context gate: text provider factory failed", exc_info=True)
        return None
    if not provider or not provider.is_configured():
        return None

    fingerprint = json.dumps(ledger.fingerprint(), ensure_ascii=False, indent=2)
    summary = (new_product_summary or "")[:_AI_SUMMARY_CAP]
    # Prompt is admin-editable via the prompt store (code/consistency_gate_prompt.txt);
    # falls back to the bundled default. ``[[KEY]]`` fill (str.replace) — the body
    # holds literal JSON braces that would break str.format.
    prompt = (
        prompt_store.get("code/consistency_gate_prompt.txt")
        .replace("[[FINGERPRINT]]", fingerprint)
        .replace("[[SUMMARY]]", summary)
        .replace("[[STEP_KEY]]", step_key or "")
    )

    try:
        result = provider.generate_text(prompt)
    except Exception as error:  # noqa: BLE001 - fail open
        logger.warning("Context gate model call raised: %s", error)
        if on_model_call:
            on_model_call(
                prompt=prompt, text=None, success=False, error=str(error),
                provider=getattr(provider, "provider_name", None),
                model=getattr(provider, "model", None),
            )
        return {"conflict": False, "conflicts": [], "summary": "一致性网关调用失败，按无冲突处理", "degraded": True}

    if on_model_call:
        on_model_call(
            prompt=prompt,
            text=result.text if result.success else None,
            success=result.success,
            error=result.error,
            provider=getattr(provider, "provider_name", None),
            model=getattr(provider, "model", None),
        )
    if not result.success:
        return {"conflict": False, "conflicts": [], "summary": "一致性网关返回失败，按无冲突处理", "degraded": True}

    parsed = _parse_json_object(result.text)
    if not parsed:
        return {"conflict": False, "verdict": "PASS", "conflicts": [],
                "summary": "一致性网关输出不可解析，按无冲突处理", "degraded": True}
    conflicts = parsed.get("conflicts") if isinstance(parsed.get("conflicts"), list) else []
    verdict = str(parsed.get("verdict") or "").upper()
    if verdict not in ("PASS", "CONCERNS", "FAIL"):
        # Back-compat with the older binary prompt ({"conflict": bool}).
        verdict = "FAIL" if parsed.get("conflict") else ("CONCERNS" if conflicts else "PASS")
    return {
        "conflict": verdict in ("CONCERNS", "FAIL"),
        "verdict": verdict,
        "conflicts": conflicts,
        "summary": str(parsed.get("summary") or ""),
    }


def _parse_json_object(text: Optional[str]) -> dict:
    """Tolerant single-object JSON parse (code-fence / prefix tolerant)."""
    if not text:
        return {}
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL | re.IGNORECASE)
    if fenced:
        cleaned = fenced.group(1).strip()
    if not cleaned.startswith("{"):
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            cleaned = cleaned[start : end + 1]
    try:
        value = json.loads(cleaned, strict=False)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


# --- persistence + events ----------------------------------------------------
def emit_context_events(
    recorder,
    step,
    *,
    det_result: dict,
    ai_result: Optional[dict],
    ledger_after: ContextLedger,
    injected_text: str = "",
) -> None:
    """Persist this step's context snapshot + check and emit the timeline event."""
    step.set_context(
        snapshot={"injected_text": injected_text, "ledger": ledger_after.to_dict()},
        check={"deterministic": det_result, "ai_gate": ai_result},
    )

    conflict = bool(ai_result and ai_result.get("conflict"))
    det_warning = det_result.get("level") == AgentEventLevel.WARNING
    if conflict or det_warning:
        summary = (ai_result or {}).get("summary") if conflict else det_result.get("summary")
        recorder.emit(
            AgentEventType.CONTEXT_CONFLICT,
            level=AgentEventLevel.WARNING,
            step_id=step.id,
            message=f"上下文一致性警告: {summary}",
            payload={"deterministic": det_result, "ai_gate": ai_result},
        )
    else:
        recorder.emit(
            AgentEventType.CONTEXT_UPDATED,
            level=AgentEventLevel.INFO,
            step_id=step.id,
            message="上下文已更新并通过一致性校验",
            payload={"deterministic": det_result, "ai_gate": ai_result},
        )
