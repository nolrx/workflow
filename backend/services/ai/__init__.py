"""
AI Services - Unified AI provider management

Usage:
    from backend.services.ai import get_text_provider, get_image_provider

    # Text generation
    provider = get_text_provider()
    result = provider.generate_text("Your prompt here")
    if result.success:
        print(result.text)

    # Image generation
    provider = get_image_provider()
    result = provider.generate_image("Generate an image of...")
    if result.success:
        image_bytes = result.image_data
"""
from backend.services.ai.base import AIProvider, ImageGenerationResult, TextGenerationResult
from backend.services.ai.factory import (
    ProviderConfig,
    ResolvedProviderConfig,
    get_image_provider,
    get_provider_config,
    get_text_provider,
    reset_providers,
)

__all__ = [
    "AIProvider",
    "TextGenerationResult",
    "ImageGenerationResult",
    "ProviderConfig",
    "ResolvedProviderConfig",
    "get_provider_config",
    "get_text_provider",
    "get_image_provider",
    "reset_providers",
]
