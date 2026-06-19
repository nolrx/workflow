"""
AI Provider Factory

Factory functions to get AI providers based on configuration.

This module routes by *capability*: text generation and image generation are
configured independently, so text can go to one API (e.g. Claude) while images
go to another (e.g. Panlaxy). `get_text_provider()` always returns a provider
built from the TEXT configuration; `get_image_provider()` always returns one
built from the IMAGE configuration. Both are thread-safe and cached.

Environment variables:
    AI_PROVIDER          Fallback provider for both text and image. Default: gemini

    Text generation:
        AI_TEXT_PROVIDER     Text provider (claude, gemini, ...). Default: AI_PROVIDER
        AI_TEXT_MODEL        Text model. Default: provider-specific
        AI_TEXT_API_KEY      Text API key. Default: provider-specific (see below)
        AI_TEXT_BASE_URL     Custom text API base URL (optional)
        AI_TEXT_MAX_TOKENS   Max output tokens for text (claude only). Default: 16000

    Image generation:
        AI_IMAGE_PROVIDER    Image provider (openai, gemini, panlaxy, ...). Default: AI_PROVIDER
        AI_IMAGE_MODEL       Image model. Default: provider-specific
        AI_IMAGE_API_KEY     Image API key. Default: provider-specific (see below)
        AI_IMAGE_BASE_URL    Custom image API base URL (optional)

    Provider-specific keys (used when the capability-specific key is unset):
        Claude:  ANTHROPIC_API_KEY / CLAUDE_API_KEY / AI_API_KEY
        Gemini:  AI_API_KEY / GOOGLE_API_KEY / GEMINI_API_KEY
        OpenAI:  OPENAI_API_KEY / AI_API_KEY (base url OPENAI_BASE_URL, model OPENAI_IMAGE_MODEL)
        Panlaxy: PANLAXY_API_KEY (base url PANLAXY_BASE_URL, model PANLAXY_IMAGE_MODEL)
"""

import logging
import os
import threading
from dataclasses import dataclass
from typing import Optional

from backend.services.ai.base import AIProvider

logger = logging.getLogger(__name__)

# Thread-safe provider caching
_text_provider: Optional[AIProvider] = None
_image_provider: Optional[AIProvider] = None
_provider_lock = threading.Lock()

# Default model configurations
DEFAULT_TEXT_MODEL = "claude-opus-4-8"
# The gemini image branch must default to a NATIVE gemini image model: the gemini
# provider calls generateContent + response_modalities=["IMAGE", ...], so a text
# model (e.g. gemini-3-flash-preview) returns only text — finish_reason=STOP with
# no image — and imagen-* models use a different `predict` API entirely. See
# .env.example. (Was wrongly "gpt-image-2", a Panlaxy model id.)
DEFAULT_GEMINI_IMAGE_MODEL = "gemini-3.1-flash-image"
DEFAULT_CLAUDE_TEXT_MODEL = "claude-opus-4-8"
# OpenAI (ChatGPT) image API — verified: gpt-image-2 works via images.generate.
DEFAULT_OPENAI_IMAGE_MODEL = "gpt-image-2"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_PANLAXY_IMAGE_MODEL = "gpt-image-2"
DEFAULT_PANLAXY_BASE_URL = "https://api.panlaxy.io/v1"


def _first(*values: Optional[str]) -> Optional[str]:
    """Return the first truthy value, or None."""
    for v in values:
        if v:
            return v
    return None


@dataclass
class ResolvedProviderConfig:
    """Fully resolved configuration for a single capability (text or image)."""

    provider_type: str
    api_key: Optional[str]
    model: str
    base_url: Optional[str] = None


@dataclass
class ProviderConfig:
    """AI provider configuration, split by capability."""

    text: ResolvedProviderConfig
    image: ResolvedProviderConfig


def _resolve_text_config() -> ResolvedProviderConfig:
    """Resolve the text-generation provider configuration from the environment."""
    default_provider = os.getenv("AI_PROVIDER", "gemini").lower()
    provider = os.getenv("AI_TEXT_PROVIDER", default_provider).lower()

    if provider == "claude":
        api_key = _first(
            os.getenv("AI_TEXT_API_KEY"),
            os.getenv("ANTHROPIC_API_KEY"),
            os.getenv("CLAUDE_API_KEY"),
            os.getenv("AI_API_KEY"),
        )
        model = os.getenv("AI_TEXT_MODEL", DEFAULT_CLAUDE_TEXT_MODEL)
        base_url = _first(os.getenv("AI_TEXT_BASE_URL"), os.getenv("ANTHROPIC_BASE_URL"))
    else:
        api_key = _first(
            os.getenv("AI_TEXT_API_KEY"),
            os.getenv("AI_API_KEY"),
            os.getenv("GOOGLE_API_KEY"),
            os.getenv("GEMINI_API_KEY"),
        )
        model = os.getenv("AI_TEXT_MODEL", DEFAULT_TEXT_MODEL)
        base_url = _first(os.getenv("AI_TEXT_BASE_URL"), os.getenv("AI_BASE_URL"))

    return ResolvedProviderConfig(
        provider_type=provider, api_key=api_key, model=model, base_url=base_url
    )


def _resolve_image_config() -> ResolvedProviderConfig:
    """Resolve the image-generation provider configuration from the environment."""
    default_provider = os.getenv("AI_PROVIDER", "gemini").lower()
    provider = os.getenv("AI_IMAGE_PROVIDER", default_provider).lower()

    if provider == "openai":
        api_key = _first(
            os.getenv("AI_IMAGE_API_KEY"),
            os.getenv("OPENAI_API_KEY"),
            os.getenv("AI_API_KEY"),
        )
        model = (
            _first(os.getenv("AI_IMAGE_MODEL"), os.getenv("OPENAI_IMAGE_MODEL"))
            or DEFAULT_OPENAI_IMAGE_MODEL
        )
        base_url = (
            _first(os.getenv("AI_IMAGE_BASE_URL"), os.getenv("OPENAI_BASE_URL"))
            or DEFAULT_OPENAI_BASE_URL
        )
    elif provider == "panlaxy":
        api_key = _first(os.getenv("AI_IMAGE_API_KEY"), os.getenv("PANLAXY_API_KEY"))
        model = (
            _first(
                os.getenv("AI_IMAGE_MODEL"),
                os.getenv("PANLAXY_IMAGE_MODEL"),
            )
            or DEFAULT_PANLAXY_IMAGE_MODEL
        )
        base_url = (
            _first(
                os.getenv("AI_IMAGE_BASE_URL"),
                os.getenv("PANLAXY_BASE_URL"),
            )
            or DEFAULT_PANLAXY_BASE_URL
        )
    else:
        api_key = _first(
            os.getenv("AI_IMAGE_API_KEY"),
            os.getenv("AI_API_KEY"),
            os.getenv("GOOGLE_API_KEY"),
            os.getenv("GEMINI_API_KEY"),
        )
        model = os.getenv("AI_IMAGE_MODEL", DEFAULT_GEMINI_IMAGE_MODEL)
        base_url = _first(os.getenv("AI_IMAGE_BASE_URL"), os.getenv("AI_BASE_URL"))

    return ResolvedProviderConfig(
        provider_type=provider, api_key=api_key, model=model, base_url=base_url
    )


def get_provider_config() -> ProviderConfig:
    """Get the full provider configuration (text + image) from the environment."""
    return ProviderConfig(text=_resolve_text_config(), image=_resolve_image_config())


def _create_provider(cfg: ResolvedProviderConfig) -> Optional[AIProvider]:
    """Create a provider instance from a resolved configuration."""
    provider = cfg.provider_type

    if provider == "claude":
        from backend.services.ai.claude import ClaudeProvider

        max_tokens = int(os.getenv("AI_TEXT_MAX_TOKENS", "32000"))
        return ClaudeProvider(
            api_key=cfg.api_key,
            model=cfg.model,
            base_url=cfg.base_url,
            max_tokens=max_tokens,
        )

    if provider == "openai":
        from backend.services.ai.openai_image import OpenAIImageProvider
        from backend.services.ai.panlaxy import (
            DEFAULT_IMAGE_QUALITY,
            DEFAULT_IMAGE_SIZE,
            DEFAULT_MAX_RETRIES,
            DEFAULT_TIMEOUT,
        )

        return OpenAIImageProvider(
            api_key=cfg.api_key,
            model=cfg.model,
            base_url=cfg.base_url,
            size=os.getenv("OPENAI_IMAGE_SIZE", DEFAULT_IMAGE_SIZE),
            quality=os.getenv("OPENAI_IMAGE_QUALITY", DEFAULT_IMAGE_QUALITY),
            timeout=float(os.getenv("OPENAI_TIMEOUT", str(DEFAULT_TIMEOUT))),
            max_retries=int(os.getenv("OPENAI_MAX_RETRIES", str(DEFAULT_MAX_RETRIES))),
        )

    if provider == "panlaxy":
        from backend.services.ai.panlaxy import (
            DEFAULT_IMAGE_QUALITY,
            DEFAULT_IMAGE_SIZE,
            DEFAULT_MAX_RETRIES,
            DEFAULT_TIMEOUT,
            PanlaxyProvider,
        )

        return PanlaxyProvider(
            api_key=cfg.api_key,
            model=cfg.model,
            base_url=cfg.base_url,
            size=os.getenv("PANLAXY_IMAGE_SIZE", DEFAULT_IMAGE_SIZE),
            quality=os.getenv("PANLAXY_IMAGE_QUALITY", DEFAULT_IMAGE_QUALITY),
            timeout=float(os.getenv("PANLAXY_TIMEOUT", str(DEFAULT_TIMEOUT))),
            max_retries=int(os.getenv("PANLAXY_MAX_RETRIES", str(DEFAULT_MAX_RETRIES))),
        )

    if provider == "gemini":
        from backend.services.ai.gemini import GeminiProvider

        return GeminiProvider(api_key=cfg.api_key, model=cfg.model)

    logger.warning(f"Unknown provider: {provider}, falling back to Gemini")
    from backend.services.ai.gemini import GeminiProvider

    return GeminiProvider(api_key=cfg.api_key, model=cfg.model)


def get_text_provider(force_new: bool = False) -> Optional[AIProvider]:
    """
    Get the configured TEXT generation provider (thread-safe).

    Args:
        force_new: Force creating a new instance instead of using cache
                   (use in background threads — see PPTTaskManager).

    Returns:
        AIProvider instance for text generation, or None if unconfigured.
    """
    global _text_provider

    if _text_provider is not None and not force_new:
        return _text_provider

    with _provider_lock:
        if _text_provider is not None and not force_new:
            return _text_provider

        cfg = _resolve_text_config()
        if not cfg.api_key:
            logger.warning("No API key configured for text provider")
            return None

        provider = _create_provider(cfg)
        if not force_new:
            _text_provider = provider
        logger.info(f"Text provider initialized: {cfg.provider_type} with model {cfg.model}")
        return provider


def get_image_provider(force_new: bool = False) -> Optional[AIProvider]:
    """
    Get the configured IMAGE generation provider (thread-safe).

    Args:
        force_new: Force creating a new instance instead of using cache
                   (use in background threads — see PPTTaskManager).

    Returns:
        AIProvider instance for image generation, or None if unconfigured.
    """
    global _image_provider

    if _image_provider is not None and not force_new:
        return _image_provider

    with _provider_lock:
        if _image_provider is not None and not force_new:
            return _image_provider

        cfg = _resolve_image_config()
        if not cfg.api_key:
            logger.warning("No API key configured for image provider")
            return None

        provider = _create_provider(cfg)
        if not force_new:
            _image_provider = provider
        logger.info(f"Image provider initialized: {cfg.provider_type} with model {cfg.model}")
        return provider


def reset_providers():
    """Reset cached providers (thread-safe, useful for testing or config changes)."""
    global _text_provider, _image_provider
    with _provider_lock:
        _text_provider = None
        _image_provider = None
    logger.info("AI providers cache reset")
