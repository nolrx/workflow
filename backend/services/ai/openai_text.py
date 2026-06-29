"""
OpenAI-compatible text provider (Chat Completions).

Implements the AIProvider TEXT interface via the `openai` SDK's
``chat.completions`` endpoint (``POST {base_url}/chat/completions``). Use this
for OpenAI-compatible gateways that serve NON-Anthropic models which only expose
the Chat Completions API and NOT the Anthropic Messages API — e.g.
``deepseek-v4-*`` on zentao.panlaxy.io (verified: ``/v1/messages`` returns 403
"HTTP node only allows access to inference API paths", while
``/v1/chat/completions`` returns 200).

Notes:
* ``base_url`` MUST include the API version segment (e.g.
  ``https://zentao.panlaxy.io/v1``); the SDK appends ``/chat/completions``. This
  differs from the Anthropic SDK / Claude CLI, which take the gateway ROOT.
* The credential is the gateway's Bearer token — the openai SDK sends ``api_key``
  as ``Authorization: Bearer``, so the token is passed as ``api_key`` here.
* Reasoning models (deepseek-v4-*) return the chain-of-thought in a separate
  ``reasoning_content`` field; the user-facing answer is in ``content``, which is
  what we extract.
"""
import base64
import logging
import os
from typing import Iterator, List, Optional

from backend.services.ai.base import (
    AIProvider,
    ImageGenerationResult,
    TextGenerationResult,
)

logger = logging.getLogger(__name__)

# Generic fallback only — the real model id comes from AI_TEXT_MODEL.
DEFAULT_OPENAI_TEXT_MODEL = "gpt-4o-mini"
DEFAULT_MAX_TOKENS = 16000
# Mirror the Claude provider's stream-gap tuning knob so ops can tune both.
DEFAULT_TIMEOUT = float(os.getenv("AI_TEXT_READ_TIMEOUT", "120"))
DEFAULT_MAX_RETRIES = int(os.getenv("AI_TEXT_OPENAI_RETRIES", "2"))


def _guess_media_type(data: bytes) -> str:
    """Best-effort image media type detection from magic bytes (for vision input)."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/png"


class OpenAITextProvider(AIProvider):
    """OpenAI-compatible text provider (chat.completions) using the openai SDK."""

    def __init__(
        self,
        api_key: Optional[str],
        model: str = DEFAULT_OPENAI_TEXT_MODEL,
        base_url: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ):
        super().__init__(api_key, model)
        self.base_url = (base_url or "").rstrip("/") or None
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = None
        self._configure()

    def _configure(self):
        """Configure the OpenAI-compatible client."""
        if not self.api_key:
            logger.warning("OpenAI text provider: API key/token not configured")
            return
        try:
            from openai import OpenAI

            kwargs = {
                "api_key": self.api_key,
                "max_retries": self.max_retries,
                "timeout": self.timeout,
            }
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = OpenAI(**kwargs)
            logger.info(
                "OpenAI text provider configured: model=%s base=%s", self.model, self.base_url
            )
        except ImportError:
            logger.error("openai package not installed. Run: pip install openai")
        except Exception as e:  # noqa: BLE001 — config failure surfaces as not-configured
            logger.error("Failed to configure OpenAI text provider: %s", e)

    @property
    def provider_name(self) -> str:
        return "openai_text"

    def is_configured(self) -> bool:
        return self._client is not None

    def _build_messages(self, prompt: str, images: Optional[List[bytes]]) -> list:
        """Build the chat messages list, attaching images as data-URL parts."""
        if not images:
            return [{"role": "user", "content": prompt}]
        parts: list = []
        for img in images:
            if not img:
                continue
            b64 = base64.standard_b64encode(img).decode("utf-8")
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{_guess_media_type(img)};base64,{b64}"},
            })
        parts.append({"type": "text", "text": prompt})
        return [{"role": "user", "content": parts}]

    def generate_text(
        self,
        prompt: str,
        images: Optional[List[bytes]] = None,
    ) -> TextGenerationResult:
        """Generate text via chat.completions, optionally with images (vision)."""
        if not self.is_configured():
            return TextGenerationResult(
                text="", success=False,
                error="OpenAI text provider not configured. Please check API key.",
            )
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                max_tokens=self.max_tokens,
                messages=self._build_messages(prompt, images),
            )
            choice = resp.choices[0]
            text = choice.message.content or ""
            if not text and getattr(choice.message, "reasoning_content", None):
                # Reasoning model ran out of budget before emitting an answer.
                logger.warning(
                    "OpenAI text: empty content (reasoning-only); finish_reason=%s — "
                    "raise AI_TEXT_MAX_TOKENS.", choice.finish_reason,
                )
            logger.debug("OpenAI text generation successful, length: %d", len(text))
            return TextGenerationResult(text=text, success=True)
        except Exception as e:  # noqa: BLE001 — model/transport errors -> result object
            return self._error_result(e)

    def generate_text_stream(
        self,
        prompt: str,
        images: Optional[List[bytes]] = None,
    ) -> Iterator[str]:
        """Stream generated text token-by-token. Raises on mid-stream errors so the
        service layer can fall back to its own default (mirrors the Claude provider)."""
        if not self.is_configured():
            return
        try:
            stream = self._client.chat.completions.create(
                model=self.model,
                max_tokens=self.max_tokens,
                messages=self._build_messages(prompt, images),
                stream=True,
            )
            for chunk in stream:
                if not chunk.choices:
                    continue
                text = getattr(chunk.choices[0].delta, "content", None)
                if text:
                    yield text
        except Exception as e:
            logger.error("OpenAI text streaming failed: %s", e)
            raise

    def generate_image(
        self,
        prompt: str,
        reference_images: Optional[List[bytes]] = None,
    ) -> ImageGenerationResult:
        """This is a TEXT provider; image generation uses the image provider."""
        return ImageGenerationResult(
            image_data=None, success=False,
            error="OpenAI text provider does not support image generation. "
                  "Use the configured image provider instead.",
        )

    @staticmethod
    def _error_result(error: Exception) -> TextGenerationResult:
        """Classify a model/transport error into a user-facing failure result."""
        error_msg = str(error)
        logger.error("OpenAI text generation failed: %s", error_msg)
        lowered = error_msg.lower()
        if any(s in lowered for s in ("authentication", "api key", "permission")) or \
                "401" in error_msg or "403" in error_msg:
            detailed = f"API authentication failed. Please check API key/token. Error: {error_msg}"
        elif "not_found" in lowered or "model" in lowered or "404" in error_msg:
            detailed = f"Model access failed. Please check model configuration. Error: {error_msg}"
        elif "rate" in lowered or "429" in error_msg:
            detailed = f"Rate limited. Please retry later. Error: {error_msg}"
        else:
            detailed = f"Text generation failed. Error: {error_msg}"
        return TextGenerationResult(text="", success=False, error=detailed)
