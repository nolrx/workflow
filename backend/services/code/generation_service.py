"""
Software creation generation service.
"""
import base64
import json
import logging
import re
from pathlib import Path
from typing import Any

from backend.services.ai import get_image_provider, get_text_provider
from backend.services.code.styles import get_styles
from backend.services.prompt_library import compose_recipe_prompt

logger = logging.getLogger(__name__)


PROMPT_DIR = Path(__file__).parent.parent.parent / "prompts" / "code"

# Matches a trailing comma right before a closing ] or } — the single most
# common reason an otherwise-valid model JSON payload fails to parse.
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


class CodeGenerationService:
    """Generate editable software creation artifacts."""

    def _load_prompt(self, name: str) -> str:
        prompt_path = PROMPT_DIR / name
        with open(prompt_path, "r", encoding="utf-8") as prompt_file:
            return prompt_file.read()

    def _generate_text(self, prompt: str, fallback: str, on_model_call=None) -> str:
        provider = get_text_provider()
        provider_name = getattr(provider, "provider_name", None)
        model_name = getattr(provider, "model", None)
        if not provider or not provider.is_configured():
            logger.warning("AI text provider is not configured; using local fallback.")
            if on_model_call:
                on_model_call(
                    prompt=prompt,
                    text=fallback,
                    success=False,
                    error="AI text provider not configured (using local fallback)",
                    provider=provider_name,
                    model=model_name,
                )
            return fallback

        result = provider.generate_text(prompt)
        if not result.success:
            logger.warning("AI text generation failed: %s", result.error)
            if on_model_call:
                on_model_call(
                    prompt=prompt,
                    text=fallback,
                    success=False,
                    error=result.error,
                    provider=provider_name,
                    model=model_name,
                )
            return fallback
        text = result.text.strip()
        if on_model_call:
            on_model_call(
                prompt=prompt,
                text=text,
                success=True,
                error=None,
                provider=provider_name,
                model=model_name,
            )
        return text

    def _generate_text_streaming(
        self, prompt: str, fallback: str, on_delta=None, on_model_call=None
    ) -> str:
        """Stream text generation chunk by chunk, returning the full text.

        ``on_delta(chunk)`` fires for every streamed chunk (used to push live
        token events). ``on_model_call(...)`` fires once at the end with the
        full text, mirroring ``_generate_text`` so the debug trace and persisted
        model_response stay consistent. Falls back to local text when the
        provider is missing or streaming fails midway.
        """
        provider = get_text_provider()
        provider_name = getattr(provider, "provider_name", None)
        model_name = getattr(provider, "model", None)
        if not provider or not provider.is_configured():
            logger.warning("AI text provider is not configured; using local fallback (stream).")
            if on_delta:
                on_delta(fallback)
            if on_model_call:
                on_model_call(
                    prompt=prompt,
                    text=fallback,
                    success=False,
                    error="AI text provider not configured (using local fallback)",
                    provider=provider_name,
                    model=model_name,
                )
            return fallback

        parts: list[str] = []
        try:
            for piece in provider.generate_text_stream(prompt):
                if not piece:
                    continue
                parts.append(piece)
                if on_delta:
                    on_delta(piece)
        except Exception as error:
            logger.warning("AI text streaming failed; using local fallback: %s", error)
            if on_model_call:
                on_model_call(
                    prompt=prompt,
                    text=fallback,
                    success=False,
                    error=str(error),
                    provider=provider_name,
                    model=model_name,
                )
            return fallback

        text = "".join(parts).strip() or fallback
        if on_model_call:
            on_model_call(
                prompt=prompt,
                text=text,
                success=True,
                error=None,
                provider=provider_name,
                model=model_name,
            )
        return text

    def _requirements_context(self, requirement: str, context_ledger: str = "") -> tuple[str, str]:
        """Build the prompt and local fallback for requirements generation."""
        prompt = self._load_prompt("requirements_prompt.txt").format(
            system_prefix=compose_recipe_prompt("product_requirement"),
            context_ledger=context_ledger,
            requirement=requirement,
        )
        fallback = (
            "# 软件需求文档\n\n"
            "## 产品定位\n"
            f"围绕用户提出的需求构建一个可快速验证的软件产品：{requirement}\n\n"
            "## 目标用户\n- 需要快速获得产品效果和开发方向的创业者、产品经理、设计师或开发者。\n\n"
            "## 核心场景\n- 用户输入业务需求。\n- 系统生成需求文档、开发流程和开发分文档。\n- 用户逐步编辑确认后生成 UI 风格缩略图和应用基调。\n\n"
            "## 功能范围\n- 需求文档生成\n- 开发流程生成\n- 文档拆分与编辑\n- 场景化提示词专家建议\n- 多风格 UI 缩略图生成\n\n"
            "## 待确认问题\n- 目标平台是 Web、移动端还是多端？\n- 是否需要真实代码生成和部署能力？\n"
        )
        return prompt, fallback

    def generate_requirements(self, requirement: str, on_model_call=None, context_ledger: str = "") -> str:
        """Generate a product requirements document from the user input."""
        prompt, fallback = self._requirements_context(requirement, context_ledger)
        return self._generate_text(prompt, fallback, on_model_call)

    def stream_requirements(
        self, requirement: str, on_delta=None, on_model_call=None, context_ledger: str = ""
    ) -> str:
        """Stream the requirements document; returns the full text when done."""
        prompt, fallback = self._requirements_context(requirement, context_ledger)
        return self._generate_text_streaming(prompt, fallback, on_delta, on_model_call)

    def _development_flow_context(
        self, requirements_doc: str, context_ledger: str = ""
    ) -> tuple[str, str]:
        """Build the prompt and local fallback for development flow generation."""
        prompt = self._load_prompt("development_flow_prompt.txt").format(
            system_prefix=compose_recipe_prompt("engineering_implementation"),
            context_ledger=context_ledger,
            requirements_doc=requirements_doc,
        )
        fallback = (
            "# 软件开发流程\n\n"
            "## 技术假设\n- 前端使用 React/TypeScript，后端使用 Flask，数据存储使用现有数据库。\n\n"
            "## 模块拆分\n- 需求输入与项目创建\n- 需求文档编辑\n- 开发流程生成\n- 开发文档拆分\n- UI 风格选择与预览\n- UI 基调确认\n\n"
            "## 开发里程碑\n"
            "1. 完成项目模型和 API。\n"
            "2. 完成单一主界面工作流。\n"
            "3. 接入风格提示词和缩略图生成。\n"
            "4. 完成用户确认与验收状态。\n\n"
            "## 验收标准\n- 用户可以从一句需求推进到可编辑文档和已确认 UI 基调。\n"
        )
        return prompt, fallback

    def generate_development_flow(
        self, requirements_doc: str, on_model_call=None, context_ledger: str = ""
    ) -> str:
        """Generate the software development process document."""
        prompt, fallback = self._development_flow_context(requirements_doc, context_ledger)
        return self._generate_text(prompt, fallback, on_model_call)

    def stream_development_flow(
        self, requirements_doc: str, on_delta=None, on_model_call=None, context_ledger: str = ""
    ) -> str:
        """Stream the development flow; returns the full text when done."""
        prompt, fallback = self._development_flow_context(requirements_doc, context_ledger)
        return self._generate_text_streaming(prompt, fallback, on_delta, on_model_call)

    def _documents_context(
        self, requirements_doc: str, development_flow: str, context_ledger: str = ""
    ) -> tuple[str, str]:
        """Build the prompt and local fallback for document splitting."""
        prompt = self._load_prompt("document_split_prompt.txt").format(
            system_prefix=compose_recipe_prompt("engineering_implementation"),
            context_ledger=context_ledger,
            requirements_doc=requirements_doc,
            development_flow=development_flow,
        )
        fallback = json.dumps(
            self._fallback_documents(requirements_doc, development_flow),
            ensure_ascii=False,
        )
        return prompt, fallback

    def _normalize_split_text(
        self, text: str, requirements_doc: str, development_flow: str
    ) -> list[dict]:
        """Parse the model's JSON output into normalized document dicts."""
        documents = self._parse_json_array(text)
        if not documents:
            return self._fallback_documents(requirements_doc, development_flow)
        return [self._normalize_document(document, index) for index, document in enumerate(documents)]

    def split_documents(
        self, requirements_doc: str, development_flow: str, on_model_call=None, context_ledger: str = ""
    ) -> list[dict]:
        """Split the flow into editable development documents."""
        prompt, fallback = self._documents_context(requirements_doc, development_flow, context_ledger)
        text = self._generate_text(prompt, fallback, on_model_call)
        return self._normalize_split_text(text, requirements_doc, development_flow)

    def stream_documents(
        self,
        requirements_doc: str,
        development_flow: str,
        on_delta=None,
        on_model_call=None,
        context_ledger: str = "",
    ) -> list[dict]:
        """Stream the raw document-split JSON; returns normalized documents when done."""
        prompt, fallback = self._documents_context(requirements_doc, development_flow, context_ledger)
        text = self._generate_text_streaming(prompt, fallback, on_delta, on_model_call)
        return self._normalize_split_text(text, requirements_doc, development_flow)

    def _style_prompt_context(
        self, requirement: str, style_ids: list[str], context_ledger: str = ""
    ) -> tuple[str, str]:
        """Build the prompt and local fallback for style document generation."""
        styles = get_styles(style_ids)
        style_text = "\n".join(
            f"- {style.name}: {style.description}\n  Prompt: {style.prompt}" for style in styles
        )
        if not style_text:
            style_text = "- Minimal SaaS: 清晰、专业、克制的产品工作台。"
        prompt = self._load_prompt("style_prompt.txt").format(
            system_prefix=compose_recipe_prompt("product_requirement"),
            context_ledger=context_ledger,
            requirement=requirement,
            styles=style_text,
        )
        fallback = (
            "# 应用风格文档\n\n"
            "## 视觉定位\n"
            f"围绕“{requirement[:120]}”构建专业、清晰、可执行的软件产品界面。\n\n"
            "## 所选风格\n"
            f"{style_text}\n\n"
            "## 缩略图生成提示词\n"
            f"Create a polished product UI screenshot for: {requirement}. {style_text}\n\n"
            "## 后续代码开发 UI 基调提示词\n"
            "界面应以清晰的信息层级、可编辑文档区、流程进度和风格预览为核心，避免营销页式空洞装饰。"
        )
        return prompt, fallback

    def generate_style_prompt(
        self, requirement: str, style_ids: list[str], on_model_call=None, context_ledger: str = ""
    ) -> str:
        """Generate a style-specific document for selected UI styles."""
        prompt, fallback = self._style_prompt_context(requirement, style_ids, context_ledger)
        return self._generate_text(prompt, fallback, on_model_call)

    def stream_style_prompt(
        self,
        requirement: str,
        style_ids: list[str],
        on_delta=None,
        on_model_call=None,
        context_ledger: str = "",
    ) -> str:
        """Stream the style document; returns the full text when done."""
        prompt, fallback = self._style_prompt_context(requirement, style_ids, context_ledger)
        return self._generate_text_streaming(prompt, fallback, on_delta, on_model_call)

    def generate_preview_images(
        self, prompt: str, count: int = 2, on_model_call=None
    ) -> list[dict[str, Any]]:
        """Generate UI preview thumbnails through the configured image provider.

        Routes through the AI factory's capability layer (``get_image_provider``)
        so previews honour ``AI_IMAGE_PROVIDER`` (Gemini by default) instead of
        hard-wiring a single vendor's HTTP endpoint. Runs inside the Agent Swarm
        thread pool, so a fresh (non-cached) provider instance is requested.
        """
        provider = get_image_provider(force_new=True)
        if provider is None or not provider.is_configured():
            raise RuntimeError("缩略图生成失败：未配置图像生成 provider，请检查图像 API Key 配置")

        provider_name = getattr(provider, "provider_name", "image")
        model_name = getattr(provider, "model", None)

        images: list[dict[str, Any]] = []
        last_error: str | None = None
        for index in range(max(1, min(count, 4))):
            result = provider.generate_image(prompt)
            if result.success and result.image_data:
                encoded = base64.b64encode(result.image_data).decode("ascii")
                images.append(
                    {
                        "id": f"preview-{index + 1}",
                        "url": f"data:image/png;base64,{encoded}",
                        "prompt": prompt,
                    }
                )
            else:
                last_error = result.error or "图像生成失败"

        # All attempts failed: surface the provider error so the caller (the
        # Agent Swarm preview step) can mark the step failed. A partial success
        # still returns whatever images we did get.
        if not images:
            raise RuntimeError(f"缩略图生成失败：{last_error or '未知错误'}")

        if on_model_call:
            on_model_call(
                prompt=prompt,
                text=f"生成 {len(images)} 张预览缩略图",
                success=True,
                error=None,
                provider=provider_name,
                model=model_name,
            )
        return images

    @staticmethod
    def _strip_code_fence(text: str) -> str:
        """Strip a Markdown code fence only when it wraps the whole output.

        The model output is usually a bare JSON array/object, but its string
        values can themselves contain ``` fences (code samples inside the
        generated docs). A naive ``` search would match an *inner* fence and
        extract garbage, so we only unwrap when the text itself starts with a
        fence.
        """
        stripped = text.strip()
        if not stripped.startswith("```"):
            return stripped
        match = re.match(
            r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", stripped, re.DOTALL | re.IGNORECASE
        )
        if match:
            return match.group(1).strip()
        # Opening fence without a clean closing one (e.g. truncated) — drop it.
        return re.sub(r"^```(?:json)?\s*\n?", "", stripped).strip()

    @staticmethod
    def _loads_tolerant(text: str):
        """``json.loads`` with a trailing-comma repair retry; None on failure.

        ``strict=False`` tolerates raw control characters (newlines/tabs) inside
        string values, and the retry strips a trailing comma before ``]``/``}``
        — together these cover the bulk of real model-emitted JSON glitches.
        """
        try:
            return json.loads(text, strict=False)
        except json.JSONDecodeError:
            repaired = _TRAILING_COMMA_RE.sub(r"\1", text)
            try:
                return json.loads(repaired, strict=False)
            except json.JSONDecodeError:
                return None

    @staticmethod
    def _extract_json_objects(text: str) -> list[str]:
        """Return every top-level ``{...}`` substring (brace-matched, string-aware).

        Used to salvage individual documents when the surrounding array framing
        is malformed or the response was truncated mid-object: each complete
        object is recovered independently, and an unterminated trailing object is
        simply skipped.
        """
        objects: list[str] = []
        depth = 0
        start: int | None = None
        in_string = False
        escape = False
        for index, char in enumerate(text):
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                if depth == 0:
                    start = index
                depth += 1
            elif char == "}" and depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    objects.append(text[start : index + 1])
                    start = None
        return objects

    def _parse_json_array(self, text: str) -> list[dict]:
        clean_text = self._strip_code_fence(text)
        # Narrow to the array span when the model wrapped it in prose.
        if not clean_text.startswith("["):
            start, end = clean_text.find("["), clean_text.rfind("]")
            if start != -1 and end > start:
                clean_text = clean_text[start : end + 1]

        value = self._loads_tolerant(clean_text)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]

        # Salvage: parse each top-level object on its own so a single malformed
        # or truncated entry doesn't discard the whole batch (which would force
        # the generic fallback documents and lose the model's real output).
        salvaged: list[dict] = []
        for chunk in self._extract_json_objects(clean_text):
            obj = self._loads_tolerant(chunk)
            if isinstance(obj, dict):
                salvaged.append(obj)
        if salvaged:
            logger.info(
                "Document split JSON was malformed; salvaged %d object(s).", len(salvaged)
            )
            return salvaged

        logger.warning(
            "Failed to parse document split JSON (length=%d); using fallback documents.",
            len(clean_text),
        )
        return []

    def _normalize_document(self, document: dict, index: int) -> dict:
        document_type = str(document.get("document_type") or f"document_{index + 1}")
        title = str(document.get("title") or f"开发文档 {index + 1}")
        content = str(document.get("content") or "请补充该部分文档内容。")
        prompt_expert = str(
            document.get("prompt_expert")
            or "你是该模块的提示词专家，请根据文档内容输出高质量、可执行的代码生成提示词。"
        )
        return {
            "document_type": document_type,
            "title": title,
            "content": content,
            "prompt_expert": prompt_expert,
            "order_index": index,
        }

    def _fallback_documents(self, requirements_doc: str, development_flow: str) -> list[dict]:
        return [
            {
                "document_type": "product_spec",
                "title": "产品需求文档",
                "content": requirements_doc,
                "prompt_expert": "你是产品需求提示词专家，请把用户需求转化为边界清晰、可验收的产品功能说明。",
                "order_index": 0,
            },
            {
                "document_type": "development_plan",
                "title": "开发流程文档",
                "content": development_flow,
                "prompt_expert": "你是技术规划提示词专家，请把开发流程拆成低风险、可连续实现的工程任务。",
                "order_index": 1,
            },
            {
                "document_type": "frontend_spec",
                "title": "前端实现文档",
                "content": "## 页面\n- 主创作页\n- 文档编辑区\n- 风格预览区\n\n## 状态\n- 当前项目\n- 当前步骤\n- 文档草稿\n- 已选风格\n",
                "prompt_expert": "你是前端提示词专家，请强调 React 组件拆分、状态管理、响应式布局和可编辑体验。",
                "order_index": 2,
            },
            {
                "document_type": "backend_spec",
                "title": "后端实现文档",
                "content": "## API\n- 创建项目\n- 更新文档\n- 生成开发流程\n- 生成风格提示词\n- 生成预览图\n\n## 约束\n- 复用认证、团队和积分体系。\n",
                "prompt_expert": "你是后端提示词专家，请强调 API 契约、数据一致性、权限校验和错误响应。",
                "order_index": 3,
            },
            {
                "document_type": "prompt_spec",
                "title": "AI 提示词链路文档",
                "content": "## 链路\n用户需求 -> 需求文档 -> 开发流程 -> 分文档 -> 风格文档 -> 缩略图提示词 -> UI 基调。\n",
                "prompt_expert": "你是 AI 提示词专家，请为每个代码生成场景定义角色、输入、输出格式和质量约束。",
                "order_index": 4,
            },
            {
                "document_type": "acceptance_plan",
                "title": "测试验收文档",
                "content": "## 验收\n- 每一步可生成、可编辑、可保存。\n- 风格多选后能生成风格文档。\n- 缩略图确认后成为 UI 基调。\n",
                "prompt_expert": "你是测试提示词专家，请生成覆盖主流程、异常状态和权限边界的验收用例。",
                "order_index": 5,
            },
        ]


_service_instance: CodeGenerationService | None = None


def get_code_generation_service() -> CodeGenerationService:
    """Return singleton code generation service."""
    global _service_instance
    if _service_instance is None:
        _service_instance = CodeGenerationService()
    return _service_instance
