"""
AI Provider Base Class

Defines the abstract interface for all AI providers.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator, List, Optional


@dataclass
class TextGenerationResult:
    """Result from text generation"""
    text: str
    success: bool
    error: Optional[str] = None


@dataclass
class ImageGenerationResult:
    """Result from image generation"""
    image_data: Optional[bytes]
    success: bool
    error: Optional[str] = None


class AIProvider(ABC):
    """Abstract base class for AI providers"""

    def __init__(self, api_key: str, model: str):
        """
        Initialize the provider.

        Args:
            api_key: API key for the provider
            model: Model name to use
        """
        self.api_key = api_key
        self.model = model

    @abstractmethod
    def generate_text(
        self,
        prompt: str,
        images: Optional[List[bytes]] = None
    ) -> TextGenerationResult:
        """
        Generate text from a prompt, optionally with images.

        Args:
            prompt: The text prompt
            images: Optional list of image bytes for multimodal input

        Returns:
            TextGenerationResult with generated text or error
        """
        pass

    @abstractmethod
    def generate_image(
        self,
        prompt: str,
        reference_images: Optional[List[bytes]] = None
    ) -> ImageGenerationResult:
        """
        Generate an image from a prompt.

        Args:
            prompt: The image generation prompt
            reference_images: Optional reference images for style guidance

        Returns:
            ImageGenerationResult with image bytes or error
        """
        pass

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Return the provider name"""
        pass

    def generate_text_stream(
        self,
        prompt: str,
        images: Optional[List[bytes]] = None
    ) -> Iterator[str]:
        """
        Stream generated text in chunks.

        Default implementation provides no true streaming: it calls
        generate_text() and yields the full text once. Providers that support
        streaming should override this. Yields nothing if generation fails so
        that callers can fall back to their own default text.

        Args:
            prompt: The text prompt
            images: Optional list of image bytes for multimodal input

        Yields:
            Text chunks as they become available
        """
        result = self.generate_text(prompt, images)
        if result.success:
            yield result.text

    def is_configured(self) -> bool:
        """Check if the provider is properly configured"""
        return bool(self.api_key)
