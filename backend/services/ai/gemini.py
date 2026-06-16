"""
Google Gemini AI Provider

Implements the AIProvider interface for Google's Gemini models.
Uses the new google.genai SDK.
"""
import logging
from typing import Iterator, List, Optional

from backend.services.ai.base import AIProvider, ImageGenerationResult, TextGenerationResult

logger = logging.getLogger(__name__)


class GeminiProvider(AIProvider):
    """Google Gemini AI provider using google.genai SDK"""

    def __init__(self, api_key: str, model: str = "gemini-3-flash-preview"):
        super().__init__(api_key, model)
        self._client = None
        self._configured = False
        self._configure()

    def _configure(self):
        """Configure the Gemini client"""
        if not self.api_key:
            logger.warning("Gemini API key not configured")
            return

        try:
            from google import genai
            self._client = genai.Client(api_key=self.api_key)
            self._configured = True
            logger.info(f"Gemini provider configured with model: {self.model}")
        except ImportError:
            logger.error("google-genai package not installed. Run: pip install google-genai")
        except Exception as e:
            logger.error(f"Failed to configure Gemini: {e}")

    @property
    def provider_name(self) -> str:
        return "gemini"

    def is_configured(self) -> bool:
        return self._configured and self._client is not None

    def generate_text(
        self,
        prompt: str,
        images: Optional[List[bytes]] = None
    ) -> TextGenerationResult:
        """
        Generate text using Gemini.

        Args:
            prompt: The text prompt
            images: Optional list of image bytes for multimodal input

        Returns:
            TextGenerationResult
        """
        if not self.is_configured():
            return TextGenerationResult(
                text="",
                success=False,
                error="Gemini provider not configured. Please check API key."
            )

        try:
            from google.genai import types

            # Build content list
            contents = [prompt]

            # Add images if provided
            if images:
                for img_data in images:
                    contents.append(types.Part.from_bytes(
                        data=img_data,
                        mime_type="image/png"
                    ))

            # Generate
            response = self._client.models.generate_content(
                model=self.model,
                contents=contents
            )
            text = response.text

            logger.debug(f"Gemini text generation successful, length: {len(text)}")
            return TextGenerationResult(text=text, success=True)

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Gemini text generation failed: {error_msg}")

            if "api_key" in error_msg.lower() or "unauthorized" in error_msg.lower():
                detailed_error = f"API authentication failed. Please check API key. Error: {error_msg}"
            elif "model" in error_msg.lower() or "404" in error_msg:
                detailed_error = f"Model access failed. Please check model configuration. Error: {error_msg}"
            else:
                detailed_error = f"Text generation failed. Error: {error_msg}"

            return TextGenerationResult(text="", success=False, error=detailed_error)

    def generate_text_stream(
        self,
        prompt: str,
        images: Optional[List[bytes]] = None
    ) -> Iterator[str]:
        """
        Stream text generation using Gemini's generate_content_stream.

        Args:
            prompt: The text prompt
            images: Optional list of image bytes for multimodal input

        Yields:
            Text chunks as they arrive. Yields nothing if the provider is not
            configured; raises on mid-stream errors so the service layer can
            fall back to local default text.
        """
        if not self.is_configured():
            return

        from google.genai import types

        # Build content list
        contents = [prompt]
        if images:
            for img_data in images:
                contents.append(types.Part.from_bytes(
                    data=img_data,
                    mime_type="image/png"
                ))

        try:
            stream = self._client.models.generate_content_stream(
                model=self.model,
                contents=contents
            )
            for chunk in stream:
                text = getattr(chunk, "text", None)
                if text:
                    yield text
        except Exception as e:
            logger.error(f"Gemini text streaming failed: {e}")
            raise

    def generate_image(
        self,
        prompt: str,
        reference_images: Optional[List[bytes]] = None
    ) -> ImageGenerationResult:
        """
        Generate image using Gemini.

        Args:
            prompt: The image generation prompt
            reference_images: Optional reference images

        Returns:
            ImageGenerationResult
        """
        if not self.is_configured():
            return ImageGenerationResult(
                image_data=None,
                success=False,
                error="Gemini provider not configured. Please check API key."
            )

        try:
            from google.genai import types

            # Build content list
            contents = [prompt]

            # Add reference images if provided
            if reference_images:
                for img_data in reference_images[:3]:  # Limit to 3 reference images
                    contents.append(types.Part.from_bytes(
                        data=img_data,
                        mime_type="image/png"
                    ))

            # Generate with image output using response_modalities
            response = self._client.models.generate_content(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE", "TEXT"]
                )
            )

            # Extract image data from response
            if response.candidates:
                for part in response.candidates[0].content.parts:
                    if part.inline_data and part.inline_data.data:
                        image_data = part.inline_data.data
                        logger.debug(f"Gemini image generation successful, size: {len(image_data)} bytes")
                        return ImageGenerationResult(image_data=image_data, success=True)

            logger.warning("No image data in Gemini response")
            return ImageGenerationResult(
                image_data=None,
                success=False,
                error="No image generated in response. The model may not support image generation."
            )

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Gemini image generation failed: {error_msg}")

            if "api_key" in error_msg.lower() or "unauthorized" in error_msg.lower():
                detailed_error = f"API authentication failed. Please check API key. Error: {error_msg}"
            elif "model" in error_msg.lower() or "404" in error_msg:
                detailed_error = f"Model access failed. Please check model configuration. Error: {error_msg}"
            else:
                detailed_error = f"Image generation failed. Error: {error_msg}"

            return ImageGenerationResult(image_data=None, success=False, error=detailed_error)
