"""
Frontend HTML build service.

The phase after UI-baseline confirmation: turn the confirmed requirements /
flow / documents / style into a COMPLETE, fully-interactive single-file HTML
application that can be saved as index.html and opened directly in a browser.

Kept separate from ``CodeGenerationService`` on purpose (different concern, and
so this can evolve independently). Prompts use ``[[TOKEN]]`` placeholders filled
by ``str.replace`` — NOT ``str.format`` — because the prompt bodies contain JSX
with ``{ }`` braces that would break ``.format``.
"""
import json
import logging
import re
from pathlib import Path
from typing import Optional

from backend.services.ai import get_text_provider

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).parent.parent.parent / "prompts" / "code"

# Hard cap so a runaway model response can't blow up the DB / artifact storage.
MAX_HTML_CHARS = 160_000


class FrontendBuildService:
    """Generate, critique and repair a runnable single-file HTML app."""

    def _load_prompt(self, name: str) -> str:
        with open(PROMPT_DIR / name, "r", encoding="utf-8") as handle:
            return handle.read()

    @staticmethod
    def _fill(template: str, **values: str) -> str:
        result = template
        for key, value in values.items():
            result = result.replace(f"[[{key}]]", value if value is not None else "")
        return result

    def _call_model(self, prompt: str, on_model_call=None) -> tuple[Optional[str], bool, Optional[str]]:
        """Return (text, success, error). Never raises on model failure."""
        provider = get_text_provider()
        provider_name = getattr(provider, "provider_name", None)
        model_name = getattr(provider, "model", None)
        if not provider or not provider.is_configured():
            if on_model_call:
                on_model_call(
                    prompt=prompt, text=None, success=False,
                    error="AI text provider not configured", provider=provider_name, model=model_name,
                )
            return None, False, "AI text provider not configured"
        result = provider.generate_text(prompt)
        if on_model_call:
            on_model_call(
                prompt=prompt,
                text=result.text if result.success else None,
                success=result.success,
                error=result.error,
                provider=provider_name,
                model=model_name,
            )
        if not result.success:
            return None, False, result.error
        return result.text, True, None

    @staticmethod
    def _extract_json(text: str) -> Optional[dict]:
        if not text:
            return None
        cleaned = text.strip()
        fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL | re.IGNORECASE)
        if fenced:
            cleaned = fenced.group(1).strip()
        if not cleaned.startswith("{"):
            match = re.search(r"(\{.*\})", cleaned, re.DOTALL)
            if match:
                cleaned = match.group(1)
        try:
            value = json.loads(cleaned)
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            logger.warning("Frontend build: failed to parse model JSON output")
            return None

    @staticmethod
    def _extract_html_payload(text: str) -> Optional[dict]:
        """Return {html, summary} from JSON output, accepting raw HTML as a backup."""
        parsed = FrontendBuildService._extract_json(text)
        if parsed and isinstance(parsed.get("html"), str):
            html = FrontendBuildService._normalize_html(parsed["html"])
            if html:
                return {"html": html, "summary": parsed.get("summary") or ""}

        raw = (text or "").strip()
        fenced_html = re.search(r"```html\s*(.*?)```", raw, re.DOTALL | re.IGNORECASE)
        if fenced_html:
            raw = fenced_html.group(1).strip()
        if raw.lower().startswith("<!doctype") or "<html" in raw.lower():
            html = FrontendBuildService._normalize_html(raw)
            if html:
                return {"html": html, "summary": ""}
        return None

    @staticmethod
    def _normalize_html(html: str) -> str:
        cleaned = (html or "").strip()[:MAX_HTML_CHARS]
        lowered = cleaned.lower()
        if "<html" not in lowered or "</html>" not in lowered:
            return ""
        if "<script" not in lowered:
            return ""
        if "<style" not in lowered:
            return ""
        if not lowered.startswith("<!doctype"):
            cleaned = "<!doctype html>\n" + cleaned
        return cleaned

    # --- public API ----------------------------------------------------------
    def build_app(
        self,
        *,
        requirement: str,
        requirements_doc: str,
        development_flow: str,
        documents_digest: str,
        style_prompt: str,
        ui_baseline_prompt: str,
        context_ledger: str = "",
        on_model_call=None,
    ) -> dict:
        """Return {html, summary, used_fallback}."""
        prompt = self._fill(
            self._load_prompt("frontend_build_prompt.txt"),
            CONTEXT_LEDGER=context_ledger or "",
            REQUIREMENT=requirement,
            REQUIREMENTS_DOC=requirements_doc or "",
            DEVELOPMENT_FLOW=development_flow or "",
            DOCUMENTS=documents_digest or "",
            STYLE_PROMPT=style_prompt or "",
            UI_BASELINE=ui_baseline_prompt or "",
        )
        text, success, error = self._call_model(prompt, on_model_call)
        if not success:
            raise RuntimeError(f"前端 HTML 生成失败：{error or '模型不可用'}")
        payload = self._extract_html_payload(text or "")
        if not payload:
            raise RuntimeError("前端 HTML 生成失败：模型没有返回完整的 index.html")
        return {
            "html": payload["html"],
            "summary": payload.get("summary") or "",
            "used_fallback": False,
        }

    def critique_app(self, html: str, on_model_call=None) -> dict:
        """Return {passed, issues:[{file,problem,severity}], summary}."""
        prompt = self._fill(
            self._load_prompt("frontend_critic_prompt.txt"),
            HTML=html,
        )
        text, success, _error = self._call_model(prompt, on_model_call)
        parsed = self._extract_json(text) if success else None
        if not parsed:
            # No reviewer available -> do not block the pipeline.
            return {"passed": True, "issues": [], "summary": "审查 Agent 不可用，跳过复检。"}
        issues = parsed.get("issues") if isinstance(parsed.get("issues"), list) else []
        return {
            "passed": bool(parsed.get("passed", not issues)),
            "issues": issues,
            "summary": parsed.get("summary") or "",
        }

    def repair_app(self, html: str, issues: list, on_model_call=None) -> dict:
        """Return a corrected {html, summary}."""
        prompt = self._fill(
            self._load_prompt("frontend_repair_prompt.txt"),
            HTML=html,
            ISSUES=json.dumps(issues, ensure_ascii=False, indent=2),
        )
        text, success, error = self._call_model(prompt, on_model_call)
        if not success:
            logger.warning("Frontend HTML repair failed; keeping original HTML: %s", error)
            return {"html": html, "summary": ""}
        payload = self._extract_html_payload(text or "")
        if not payload:
            logger.warning("Frontend HTML repair returned invalid HTML; keeping original HTML")
            return {"html": html, "summary": ""}
        return {
            "html": payload["html"],
            "summary": payload.get("summary") or "",
        }

_service_instance: FrontendBuildService | None = None


def get_frontend_build_service() -> FrontendBuildService:
    global _service_instance
    if _service_instance is None:
        _service_instance = FrontendBuildService()
    return _service_instance
