"""
Anthropic Claude AI Provider (text generation)

Implements the AIProvider interface for Anthropic's Claude models using the
official `anthropic` SDK. This provider is used for TEXT generation only
(outlines, page descriptions, social content). Image generation is handled by
a dedicated image provider (see factory.get_image_provider).
"""
import logging
import os
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

# Max gap (seconds) between streamed chunks before the connection is considered
# dead. Generation streams deltas/pings continuously, so a gap this long is a
# stall, not slow output. Overridable via env for ops tuning.
READ_TIMEOUT = float(os.getenv("AI_TEXT_READ_TIMEOUT", "120"))

# Retry transient transport failures that surface WHILE consuming the stream
# body — e.g. "peer closed connection without sending complete message body
# (incomplete chunked read)". These happen on long single-shot generations
# (a big single-file build is much longer than the old multi-file approach).
# The SDK's built-in max_retries only covers establishing the initial request,
# NOT mid-stream drops, so we retry the whole generation here. There is no
# partial result to salvage on a dropped stream, so a clean re-run is correct.
# Overridable via env for ops tuning.
STREAM_MAX_RETRIES = int(os.getenv("AI_TEXT_STREAM_RETRIES", "2"))
STREAM_RETRY_BASE_DELAY = float(os.getenv("AI_TEXT_STREAM_RETRY_DELAY", "2"))
STREAM_RETRY_MAX_DELAY = 15.0


def _resolve_use_thinking(model: str) -> bool:
    """Whether to send the Claude-specific adaptive ``thinking`` parameter.

    Adaptive thinking is a Claude feature. A non-Claude model served through an
    Anthropic-compatible gateway (e.g. ``deepseek-v4-*`` on zentao.panlaxy.io)
    typically rejects the param, which would fail every text step. ``AI_TEXT_THINKING``:
    ``auto`` (default) -> on IFF the model id starts with ``claude``; ``on`` /
    ``off`` force it. So existing Claude setups are unchanged.
    """
    mode = os.getenv("AI_TEXT_THINKING", "auto").strip().lower()
    if mode in ("on", "1", "true", "yes"):
        return True
    if mode in ("off", "0", "false", "no"):
        return False
    return (model or "").lower().startswith("claude")


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
        api_key: Optional[str] = None,
        model: str = DEFAULT_CLAUDE_MODEL,
        base_url: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        auth_token: Optional[str] = None,
    ):
        super().__init__(api_key, model)
        # auth_token (Authorization: Bearer) is used against third-party gateways
        # such as https://zentao.panlaxy.io; api_key (x-api-key) is the official
        # API. Exactly one is sent — see _configure.
        self.auth_token = auth_token
        self.base_url = base_url
        self.max_tokens = max_tokens
        # Adaptive thinking is Claude-only; disabled for non-Claude gateway models.
        self._use_thinking = _resolve_use_thinking(model)
        self._client = None
        self._configured = False
        self._configure()

    def _thinking_kwargs(self) -> dict:
        """Extra messages.stream() kwargs for adaptive thinking (empty when off)."""
        return {"thinking": {"type": "adaptive"}} if self._use_thinking else {}

    def _configure(self):
        """Configure the Anthropic client."""
        if not (self.api_key or self.auth_token):
            logger.warning("Claude credential not configured (no API key or auth token)")
            return

        try:
            import anthropic
            import httpx

            # Prefer the Bearer auth token when present and DO NOT also pass an
            # api_key: the SDK would then send both an Authorization and an
            # x-api-key header, which the API/gateway rejects (401).
            if self.auth_token:
                kwargs = {"auth_token": self.auth_token}
            else:
                kwargs = {"api_key": self.api_key}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            # Bound every request with a finite timeout. Streaming delivers chunks
            # (text deltas + periodic pings) every few seconds, so a 120s read gap
            # means the connection is dead — without this the SDK default lets a
            # stalled stream hang the worker thread (and thus the whole AgentRun)
            # indefinitely, permanently burning one of the runtime's 4 slots.
            kwargs["timeout"] = httpx.Timeout(READ_TIMEOUT, connect=10.0, write=30.0, pool=10.0)
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

        import time

        import anthropic
        import httpx

        # Transient transport failures (mid-stream connection drops, read
        # stalls, connection resets) are worth retrying as a whole-generation
        # re-run; everything else is classified and surfaced immediately.
        retryable = (httpx.TransportError, anthropic.APIConnectionError)

        messages = [{"role": "user", "content": self._build_content(prompt, images)}]
        last_error: Optional[Exception] = None

        for attempt in range(STREAM_MAX_RETRIES + 1):
            try:
                # Stream + adaptive thinking: recommended setup for Opus 4.8.
                # Streaming protects against HTTP timeouts on long generations.
                with self._client.messages.stream(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    messages=messages,
                    **self._thinking_kwargs(),
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

                if message.stop_reason == "max_tokens":
                    # Output is truncated at the token ceiling — common when a
                    # single-file build needs more room than the default.
                    logger.warning(
                        "Claude output hit max_tokens=%s and is truncated; "
                        "raise AI_TEXT_MAX_TOKENS for long single-file generations.",
                        self.max_tokens,
                    )

                logger.debug(f"Claude text generation successful, length: {len(text)}")
                return TextGenerationResult(text=text, success=True)

            except retryable as e:
                last_error = e
                if attempt < STREAM_MAX_RETRIES:
                    delay = min(STREAM_RETRY_BASE_DELAY * (2 ** attempt), STREAM_RETRY_MAX_DELAY)
                    logger.warning(
                        "Claude stream interrupted (attempt %d/%d): %s — retrying in %.1fs",
                        attempt + 1, STREAM_MAX_RETRIES + 1, e, delay,
                    )
                    time.sleep(delay)
                    continue
                logger.error(
                    "Claude stream failed after %d attempts: %s",
                    STREAM_MAX_RETRIES + 1, e,
                )
                break
            except Exception as e:
                last_error = e
                break

        return self._error_result(last_error)

    @staticmethod
    def _error_result(error: Optional[Exception]) -> TextGenerationResult:
        """Classify a model/transport error into a user-facing failure result."""
        error_msg = str(error)
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
                messages=messages,
                **self._thinking_kwargs(),
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
