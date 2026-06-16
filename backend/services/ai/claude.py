"""
Anthropic Claude AI Provider (text generation)

Implements the AIProvider interface for Anthropic's Claude models using the
official `anthropic` SDK. This provider is used for TEXT generation only
(outlines, page descriptions, social content). Image generation is handled by
a dedicated image provider (see factory.get_image_provider).
"""
import logging
from typing import List, Optional

from backend.services.ai.base import (
    AIProvider,
    ImageGenerationResult,
    TextGenerationResult,
)

logger = logging.getLogger(__name__)

# Default model for Claude text generation.
DEFAULT_CLAUDE_MODEL = "claude-opus-4-8"

# Streaming is used for all requests so large outputs don't hit HTTP timeouts.
DEFAULT_MAX_TOKENS = 16000


def _guess_media_type(data: bytes) -> str:
    """Best-effort image media type detection from magic bytes."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/png"


class ClaudeProvider(AIProvider):
    """Anthropic Claude provider using the official anthropic SDK."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_CLAUDE_MODEL,
        base_url: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ):
        super().__init__(api_key, model)
        self.base_url = base_url
        self.max_tokens = max_tokens
        self._client = None
        self._configured = False
        self._configure()

    def _configure(self):
        """Configure the Anthropic client."""
        if not self.api_key:
            logger.warning("Claude API key not configured")
            return

        try:
            import anthropic

            kwargs = {"api_key": self.api_key}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self._client = anthropic.Anthropic(**kwargs)
            self._configured = True
            logger.info(f"Claude provider configured with model: {self.model}")
        except ImportError:
            logger.error("anthropic package not installed. Run: pip install anthropic")
        except Exception as e:
            logger.error(f"Failed to configure Claude: {e}")

    @property
    def provider_name(self) -> str:
        return "claude"

    def is_configured(self) -> bool:
        return self._configured and self._client is not None

    def generate_text(
        self,
        prompt: str,
        images: Optional[List[bytes]] = None,
    ) -> TextGenerationResult:
        """
        Generate text using Claude, optionally with images (vision input).

        Args:
            prompt: The text prompt
            images: Optional list of image bytes for multimodal input

        Returns:
            TextGenerationResult with generated text or error
        """
        if not self.is_configured():
            return TextGenerationResult(
                text="",
                success=False,
                error="Claude provider not configured. Please check API key.",
            )

        try:
            messages = [{"role": "user", "content": self._build_content(prompt, images)}]

            # Stream + adaptive thinking: recommended setup for Opus 4.8.
            # Streaming protects against HTTP timeouts on long generations.
            with self._client.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                thinking={"type": "adaptive"},
                messages=messages,
            ) as stream:
                message = stream.get_final_message()

            if message.stop_reason == "refusal":
                detail = getattr(getattr(message, "stop_details", None), "explanation", None)
                logger.warning(f"Claude refused the request: {detail}")
                return TextGenerationResult(
                    text="",
                    success=False,
                    error=f"Request was declined for safety reasons. {detail or ''}".strip(),
                )

            text = "".join(
                block.text for block in message.content if block.type == "text"
            )

            logger.debug(f"Claude text generation successful, length: {len(text)}")
            return TextGenerationResult(text=text, success=True)

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Claude text generation failed: {error_msg}")

            lowered = error_msg.lower()
            if "authentication" in lowered or "api_key" in lowered or "401" in error_msg:
                detailed_error = f"API authentication failed. Please check API key. Error: {error_msg}"
            elif "not_found" in lowered or "model" in lowered or "404" in error_msg:
                detailed_error = f"Model access failed. Please check model configuration. Error: {error_msg}"
            elif "rate_limit" in lowered or "429" in error_msg:
                detailed_error = f"Rate limited. Please retry later. Error: {error_msg}"
            else:
                detailed_error = f"Text generation failed. Error: {error_msg}"

            return TextGenerationResult(text="", success=False, error=detailed_error)

    def _build_content(self, prompt: str, images: Optional[List[bytes]]) -> list:
        """Build a user-message content list, prepending any images as blocks."""
        import base64

        content: list = []
        if images:
            for img_data in images:
                if not img_data:
                    continue
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": _guess_media_type(img_data),
                        "data": base64.standard_b64encode(img_data).decode("utf-8"),
                    },
                })
        content.append({"type": "text", "text": prompt})
        return content

    def generate_text_stream(self, prompt: str, images: Optional[List[bytes]] = None):
        """
        Stream generated text token-by-token via the Claude streaming API.

        Yields text deltas as they arrive. Yields nothing if the provider is not
        configured; raises on mid-stream errors so the service layer can fall
        back to its own default text (mirrors the Gemini provider's contract).
        """
        if not self.is_configured():
            return

        messages = [{"role": "user", "content": self._build_content(prompt, images)}]
        try:
            with self._client.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                thinking={"type": "adaptive"},
                messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    if text:
                        yield text
        except Exception as e:
            logger.error(f"Claude text streaming failed: {e}")
            raise

    def generate_image(
        self,
        prompt: str,
        reference_images: Optional[List[bytes]] = None,
    ) -> ImageGenerationResult:
        """
        Claude is a text provider. Image generation must use the configured
        image provider (see factory.get_image_provider).
        """
        return ImageGenerationResult(
            image_data=None,
            success=False,
            error="Claude provider does not support image generation. "
                  "Use the configured image provider instead.",
        )
