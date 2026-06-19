"""
Panlaxy Image AI Provider (image generation)

Implements the AIProvider interface for the Panlaxy image API, which is
OpenAI-compatible. Uses the official `openai` SDK pointed at the Panlaxy base
URL (the same approach verified in the standalone generate_image.py test):

    client.images.generate(...)   # text-to-image
    client.images.edit(...)       # image edit / continuity with reference images

This provider is used for IMAGE generation only. Text generation is handled by
a dedicated text provider (see factory.get_text_provider).
"""
import base64
import logging
from typing import List, Optional

from backend.services.ai.base import (
    AIProvider,
    ImageGenerationResult,
    TextGenerationResult,
)

logger = logging.getLogger(__name__)

DEFAULT_PANLAXY_BASE_URL = "https://api.panlaxy.io/v1"
DEFAULT_PANLAXY_MODEL = "gpt-image-2"
DEFAULT_IMAGE_SIZE = "1024x1024"
DEFAULT_IMAGE_QUALITY = "medium"  # low | medium | high | auto
# Image generation is slow; allow a generous timeout.
DEFAULT_TIMEOUT = 180.0
DEFAULT_MAX_RETRIES = 0
# Reference images are sent to the edits endpoint; cap to avoid huge payloads.
MAX_REFERENCE_IMAGES = 3


class PanlaxyProvider(AIProvider):
    """Panlaxy (OpenAI-compatible) image provider using the openai SDK."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_PANLAXY_MODEL,
        base_url: Optional[str] = None,
        size: str = DEFAULT_IMAGE_SIZE,
        quality: str = DEFAULT_IMAGE_QUALITY,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ):
        super().__init__(api_key, model)
        self.base_url = (base_url or DEFAULT_PANLAXY_BASE_URL).rstrip("/")
        self.size = size
        self.quality = quality
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = None
        self._configure()

    def _configure(self):
        """Configure the OpenAI-compatible client (Panlaxy or, for subclasses, OpenAI)."""
        if not self.api_key:
            logger.warning(f"{self.provider_name} API key not configured")
            return
        try:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                max_retries=self.max_retries,
                timeout=self.timeout,
            )
            logger.info(
                f"{self.provider_name} image provider configured with model: "
                f"{self.model} at {self.base_url}"
            )
        except ImportError:
            logger.error("openai package not installed. Run: pip install openai")
        except Exception as e:
            logger.error(f"Failed to configure {self.provider_name} client: {e}")

    @property
    def provider_name(self) -> str:
        return "panlaxy"

    def is_configured(self) -> bool:
        return self._client is not None

    @staticmethod
    def _extract_image_bytes(item) -> Optional[bytes]:
        """Pull image bytes out of an OpenAI-style image data item."""
        b64 = getattr(item, "b64_json", None)
        if b64:
            return base64.b64decode(b64)

        url = getattr(item, "url", None)
        if url:
            try:
                import httpx

                resp = httpx.get(url, timeout=DEFAULT_TIMEOUT)
                resp.raise_for_status()
                return resp.content
            except Exception as e:
                logger.error(f"Failed to download generated image from URL: {e}")
                return None

        return None

    def generate_image(
        self,
        prompt: str,
        reference_images: Optional[List[bytes]] = None,
    ) -> ImageGenerationResult:
        """
        Generate an image. With reference images, the OpenAI-compatible image
        edit endpoint is used (for style/continuity); otherwise text-to-image.

        Args:
            prompt: The image generation prompt
            reference_images: Optional reference images for style/continuity

        Returns:
            ImageGenerationResult with image bytes or error
        """
        if not self.is_configured():
            return ImageGenerationResult(
                image_data=None,
                success=False,
                error="Panlaxy provider not configured. Please check API key.",
            )

        refs = [img for img in (reference_images or []) if img][:MAX_REFERENCE_IMAGES]

        try:
            if refs:
                result = self._client.images.edit(
                    model=self.model,
                    image=[(f"ref_{i}.png", img, "image/png") for i, img in enumerate(refs)],
                    prompt=prompt,
                    size=self.size,
                    quality=self.quality,
                )
            else:
                result = self._client.images.generate(
                    model=self.model,
                    prompt=prompt,
                    size=self.size,
                    quality=self.quality,
                )

            if not result.data:
                logger.warning("No image data in Panlaxy response")
                return ImageGenerationResult(
                    image_data=None, success=False, error="No image data in response."
                )

            image_data = self._extract_image_bytes(result.data[0])
            if image_data:
                logger.debug(f"Panlaxy image generation successful, size: {len(image_data)} bytes")
                return ImageGenerationResult(image_data=image_data, success=True)

            return ImageGenerationResult(
                image_data=None, success=False, error="No image data in response."
            )

        except Exception as e:
            error_msg = self._format_error(e)
            logger.error(f"Panlaxy image generation failed: {error_msg}")
            return ImageGenerationResult(
                image_data=None, success=False,
                error=f"Image generation failed: {error_msg}",
            )

    @staticmethod
    def _format_error(exc: Exception) -> str:
        """Build a concise error string from an openai SDK exception."""
        status = getattr(exc, "status_code", None)
        message = getattr(exc, "message", None) or str(exc)
        return f"[{status}] {message}" if status else message

    def generate_text(
        self,
        prompt: str,
        images: Optional[List[bytes]] = None,
    ) -> TextGenerationResult:
        """
        Panlaxy is configured here as an image provider. Text generation must
        use the configured text provider (see factory.get_text_provider).
        """
        return TextGenerationResult(
            text="",
            success=False,
            error="Panlaxy image provider does not support text generation. "
                  "Use the configured text provider instead.",
        )
