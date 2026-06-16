"""
Frontend project build service.

The phase after UI-baseline confirmation: turn the confirmed requirements /
flow / documents / style into a COMPLETE, fully-interactive React + TypeScript
single-page app, returned as a Sandpack-compatible file map.

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

# Hard cap so a runaway model response can't blow up the DB / Sandpack.
MAX_FILES = 24
MAX_FILE_CHARS = 24_000


class FrontendBuildService:
    """Generate, critique and repair a runnable React/TS frontend project."""

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
    def _normalize_files(raw_files) -> dict[str, str]:
        """Coerce a {path: content|{code}} map into {/path: content} for Sandpack."""
        files: dict[str, str] = {}
        if not isinstance(raw_files, dict):
            return files
        for path, content in list(raw_files.items())[:MAX_FILES]:
            if not isinstance(path, str):
                continue
            if isinstance(content, dict):
                content = content.get("code") or content.get("content") or ""
            if not isinstance(content, str):
                continue
            norm_path = path if path.startswith("/") else f"/{path}"
            files[norm_path] = content[:MAX_FILE_CHARS]
        return files

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
        on_model_call=None,
    ) -> dict:
        """Return {files, entry, components, summary, used_fallback}."""
        prompt = self._fill(
            self._load_prompt("frontend_build_prompt.txt"),
            REQUIREMENT=requirement,
            REQUIREMENTS_DOC=requirements_doc or "",
            DEVELOPMENT_FLOW=development_flow or "",
            DOCUMENTS=documents_digest or "",
            STYLE_PROMPT=style_prompt or "",
            UI_BASELINE=ui_baseline_prompt or "",
        )
        text, success, _error = self._call_model(prompt, on_model_call)
        parsed = self._extract_json(text) if success else None
        files = self._normalize_files(parsed.get("files")) if parsed else {}
        if not files or "/App.tsx" not in files:
            fallback = self._fallback_app(requirement)
            fallback["used_fallback"] = True
            return fallback
        return {
            "files": files,
            "entry": parsed.get("entry") or "/App.tsx",
            "components": parsed.get("components") or sorted(files.keys()),
            "summary": parsed.get("summary") or "",
            "used_fallback": False,
        }

    def critique_app(self, files: dict[str, str], on_model_call=None) -> dict:
        """Return {passed, issues:[{file,problem,severity}], summary}."""
        prompt = self._fill(
            self._load_prompt("frontend_critic_prompt.txt"),
            FILES=self._files_to_text(files),
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

    def repair_app(self, files: dict[str, str], issues: list, on_model_call=None) -> dict:
        """Return a corrected {files, entry, components, summary}."""
        prompt = self._fill(
            self._load_prompt("frontend_repair_prompt.txt"),
            FILES=self._files_to_text(files),
            ISSUES=json.dumps(issues, ensure_ascii=False, indent=2),
        )
        text, success, _error = self._call_model(prompt, on_model_call)
        parsed = self._extract_json(text) if success else None
        repaired = self._normalize_files(parsed.get("files")) if parsed else {}
        if not repaired or "/App.tsx" not in repaired:
            # Repair failed; keep the original files rather than losing work.
            return {"files": files, "entry": "/App.tsx", "components": sorted(files.keys()), "summary": ""}
        return {
            "files": repaired,
            "entry": parsed.get("entry") or "/App.tsx",
            "components": parsed.get("components") or sorted(repaired.keys()),
            "summary": parsed.get("summary") or "",
        }

    @staticmethod
    def _files_to_text(files: dict[str, str]) -> str:
        parts = []
        for path, content in files.items():
            parts.append(f"=== FILE: {path} ===\n{content}")
        return "\n\n".join(parts)

    @staticmethod
    def _fallback_app(requirement: str) -> dict:
        """A minimal but FULLY interactive app used when AI is unavailable."""
        title = (requirement or "Demo App").strip()[:60].replace("`", "'")
        app_tsx = (
            "import { useState } from \"react\";\n"
            "import \"./styles.css\";\n\n"
            "interface Task { id: number; text: string; done: boolean; }\n\n"
            "export default function App() {\n"
            "  const [tasks, setTasks] = useState<Task[]>([\n"
            "    { id: 1, text: \"体验可交互的待办\", done: false },\n"
            "  ]);\n"
            "  const [draft, setDraft] = useState(\"\");\n"
            "  const add = () => {\n"
            "    const text = draft.trim();\n"
            "    if (!text) return;\n"
            "    setTasks((t) => [...t, { id: Date.now(), text, done: false }]);\n"
            "    setDraft(\"\");\n"
            "  };\n"
            "  const toggle = (id: number) =>\n"
            "    setTasks((t) => t.map((x) => (x.id === id ? { ...x, done: !x.done } : x)));\n"
            "  const remove = (id: number) => setTasks((t) => t.filter((x) => x.id !== id));\n"
            "  const remaining = tasks.filter((t) => !t.done).length;\n"
            "  return (\n"
            "    <main className=\"app\">\n"
            f"      <h1>{title}</h1>\n"
            "      <p className=\"muted\">{remaining} 项待完成</p>\n"
            "      <div className=\"row\">\n"
            "        <input\n"
            "          value={draft}\n"
            "          onChange={(e) => setDraft(e.target.value)}\n"
            "          onKeyDown={(e) => e.key === \"Enter\" && add()}\n"
            "          placeholder=\"输入后回车添加\"\n"
            "        />\n"
            "        <button onClick={add}>添加</button>\n"
            "      </div>\n"
            "      <ul>\n"
            "        {tasks.map((task) => (\n"
            "          <li key={task.id} className={task.done ? \"done\" : \"\"}>\n"
            "            <label>\n"
            "              <input type=\"checkbox\" checked={task.done} onChange={() => toggle(task.id)} />\n"
            "              <span>{task.text}</span>\n"
            "            </label>\n"
            "            <button className=\"link\" onClick={() => remove(task.id)}>删除</button>\n"
            "          </li>\n"
            "        ))}\n"
            "      </ul>\n"
            "    </main>\n"
            "  );\n"
            "}\n"
        )
        styles_css = (
            ".app{max-width:520px;margin:40px auto;padding:24px;font-family:ui-sans-serif,system-ui;"
            "color:#0f172a}\n"
            "h1{font-size:22px;margin:0 0 4px}\n"
            ".muted{color:#64748b;margin:0 0 16px;font-size:14px}\n"
            ".row{display:flex;gap:8px;margin-bottom:16px}\n"
            "input[type=text],.row input{flex:1;padding:8px 10px;border:1px solid #cbd5e1;border-radius:8px}\n"
            "button{padding:8px 14px;border:0;border-radius:8px;background:#2563eb;color:#fff;cursor:pointer}\n"
            "button.link{background:none;color:#ef4444;padding:4px 8px}\n"
            "ul{list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:8px}\n"
            "li{display:flex;align-items:center;justify-content:space-between;border:1px solid #e2e8f0;"
            "border-radius:8px;padding:8px 12px}\n"
            "li.done span{text-decoration:line-through;color:#94a3b8}\n"
            "label{display:flex;align-items:center;gap:8px;cursor:pointer}\n"
        )
        return {
            "files": {"/App.tsx": app_tsx, "/styles.css": styles_css},
            "entry": "/App.tsx",
            "components": ["/App.tsx", "/styles.css"],
            "summary": "AI 不可用，使用内置可交互待办示例作为占位前端。",
        }


_service_instance: FrontendBuildService | None = None


def get_frontend_build_service() -> FrontendBuildService:
    global _service_instance
    if _service_instance is None:
        _service_instance = FrontendBuildService()
    return _service_instance
