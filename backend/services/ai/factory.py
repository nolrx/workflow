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
from dataclasses import dataclass, replace
from typing import Optional

from backend.services.ai.base import AIProvider

logger = logging.getLogger(__name__)

# Thread-safe provider caching
_text_provider: Optional[AIProvider] = None
_image_provider: Optional[AIProvider] = None
# Per-role text providers for role-specific model/credential overrides (see
# text_role_config / get_text_provider(role=...)). Keyed by role so a
# reasoning-vs-fast split doesn't rebuild the client on every call.
_text_providers_by_role: dict = {}
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
DEFAULT_GEMINI_TEXT_MODEL = "gemini-3-flash-preview"
# Providers that can generate text. "openai" here means an OpenAI-compatible
# chat-completions endpoint (gateways serving non-Anthropic models, e.g.
# deepseek-v4-* on zentao.panlaxy.io) — distinct from the "openai" IMAGE branch,
# which is disambiguated by capability in _create_provider. panlaxy is image-only.
TEXT_PROVIDERS = ("claude", "gemini", "openai")
# Generic fallback for an OpenAI-compatible text gateway; real id from AI_TEXT_MODEL.
DEFAULT_OPENAI_TEXT_MODEL = "gpt-4o-mini"
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


def _resolve_text_auth_token() -> Optional[str]:
    """Bearer auth token for the Claude text provider (third-party gateways).

    Used as ``Authorization: Bearer`` against an Anthropic-compatible gateway
    such as ``https://zentao.panlaxy.io``; when set it takes precedence over the
    api_key (x-api-key) at the client layer. Capability-specific override first,
    then the standard ``ANTHROPIC_AUTH_TOKEN`` the anthropic SDK / Claude Code
    CLI also read.
    """
    return _first(os.getenv("AI_TEXT_AUTH_TOKEN"), os.getenv("ANTHROPIC_AUTH_TOKEN"))


@dataclass
class ResolvedProviderConfig:
    """Fully resolved configuration for a single capability (text or image)."""

    provider_type: str
    api_key: Optional[str]
    model: str
    base_url: Optional[str] = None
    # Bearer auth token (Authorization: Bearer) for third-party Claude gateways
    # such as https://zentao.panlaxy.io. Mutually exclusive with api_key at the
    # client layer — see ClaudeProvider._configure. Text/Claude only.
    auth_token: Optional[str] = None


@dataclass
class ProviderConfig:
    """AI provider configuration, split by capability."""

    text: ResolvedProviderConfig
    image: ResolvedProviderConfig


def _resolve_text_config() -> ResolvedProviderConfig:
    """Resolve the text-generation provider configuration from the environment."""
    default_provider = os.getenv("AI_PROVIDER", "gemini").lower()
    provider = os.getenv("AI_TEXT_PROVIDER", default_provider).lower()

    auth_token: Optional[str] = None
    if provider == "claude":
        api_key = _first(
            os.getenv("AI_TEXT_API_KEY"),
            os.getenv("ANTHROPIC_API_KEY"),
            os.getenv("CLAUDE_API_KEY"),
            os.getenv("AI_API_KEY"),
        )
        # Bearer auth for third-party gateways (e.g. https://zentao.panlaxy.io).
        # When set it takes precedence over api_key at the client layer.
        auth_token = _resolve_text_auth_token()
        model = os.getenv("AI_TEXT_MODEL", DEFAULT_CLAUDE_TEXT_MODEL)
        base_url = _first(os.getenv("AI_TEXT_BASE_URL"), os.getenv("ANTHROPIC_BASE_URL"))
    elif provider == "openai":
        # OpenAI-compatible chat-completions gateway (e.g. deepseek-v4-* on
        # zentao.panlaxy.io, which only serves /v1/chat/completions — not the
        # Anthropic /v1/messages the claude path uses). The Bearer token is the
        # openai SDK's api_key, so AI_TEXT_AUTH_TOKEN is accepted here too.
        # base_url MUST include /v1 (the SDK appends /chat/completions).
        api_key = _first(
            os.getenv("AI_TEXT_API_KEY"),
            os.getenv("AI_TEXT_AUTH_TOKEN"),
            os.getenv("OPENAI_API_KEY"),
            os.getenv("AI_API_KEY"),
        )
        model = os.getenv("AI_TEXT_MODEL", DEFAULT_OPENAI_TEXT_MODEL)
        base_url = (
            _first(os.getenv("AI_TEXT_BASE_URL"), os.getenv("OPENAI_BASE_URL"))
            or DEFAULT_OPENAI_BASE_URL
        )
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
        provider_type=provider, api_key=api_key, model=model,
        base_url=base_url, auth_token=auth_token,
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


def _create_provider(cfg: ResolvedProviderConfig, capability: str = "text") -> Optional[AIProvider]:
    """Create a provider instance from a resolved configuration.

    ``capability`` ("text" | "image") disambiguates the "openai" provider type,
    which has both a text (chat-completions) and an image implementation.
    """
    provider = cfg.provider_type

    if provider == "claude":
        from backend.services.ai.claude import ClaudeProvider

        max_tokens = int(os.getenv("AI_TEXT_MAX_TOKENS", "32000"))
        return ClaudeProvider(
            api_key=cfg.api_key,
            model=cfg.model,
            base_url=cfg.base_url,
            max_tokens=max_tokens,
            auth_token=cfg.auth_token,
        )

    if provider == "openai":
        if capability != "image":
            # OpenAI-compatible chat-completions text provider (deepseek-v4-* etc.).
            from backend.services.ai.openai_text import OpenAITextProvider

            return OpenAITextProvider(
                api_key=cfg.api_key,
                model=cfg.model,
                base_url=cfg.base_url,
                max_tokens=int(os.getenv("AI_TEXT_MAX_TOKENS", "32000")),
            )

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


def text_model_for(role: Optional[str]) -> Optional[str]:
    """The text model id for a task ROLE (or the default when role is None/unknown).
    Thin accessor over :func:`text_role_config` for callers that only need the id
    (logging / metering)."""
    return text_role_config(role).get("model") or os.getenv("AI_TEXT_MODEL")


def text_role_config(role: Optional[str]) -> dict:
    """Env-driven model + credential overrides for a text task ROLE.

    Different Code-domain tasks want different model TIERS on the same text gateway,
    and on this gateway a token is authorized per-model — so a role override must
    carry its OWN credential, not just a model id:

      * ``'reasoning'`` — deep document synthesis (requirements / flow / documents /
        style). Needs a reasoning model → ``AI_TEXT_REASONING_*``.
      * ``'fast'`` — fast, structured, mechanical work a reasoning model over-thinks
        / times out on: the backlog planner's per-FR task split, and (future)
        large-file slicing → ``AI_TEXT_FAST_*``.

    Reads ``AI_TEXT_<ROLE>_MODEL`` / ``_AUTH_TOKEN`` / ``_BASE_URL``; each key that is
    unset falls back to the default ``AI_TEXT_*`` at build time, so a single-model
    deploy keeps working unchanged. Returns only the keys that are explicitly set
    (empty dict for the default role).
    """
    if not role:
        return {}
    prefix = f"AI_TEXT_{role.upper()}_"
    out: dict = {}
    model = os.getenv(prefix + "MODEL")
    if model:
        out["model"] = model
    token = os.getenv(prefix + "AUTH_TOKEN")
    if token:
        out["auth_token"] = token
    base_url = os.getenv(prefix + "BASE_URL")
    if base_url:
        out["base_url"] = base_url
    return out


def get_text_provider(
    force_new: bool = False, role: Optional[str] = None
) -> Optional[AIProvider]:
    """
    Get the configured TEXT generation provider (thread-safe).

    Args:
        force_new: Force creating a new instance instead of using cache
                   (use in background threads — see the agent runtime).
        role: Route to a role-specific model tier + credential (see
              ``text_role_config``): ``'reasoning'`` for doc synthesis, ``'fast'``
              for the planner / slicing. Cached per-role. ``None`` = the default
              ``AI_TEXT_*`` model and the default single-provider cache.

    Returns:
        AIProvider instance for text generation, or None if unconfigured.
    """
    global _text_provider

    # Role-specific override: build (and per-role-cache) a provider on the text
    # config with the role's model + credential (token per-model on this gateway).
    overrides = text_role_config(role)
    if overrides:
        cached = _text_providers_by_role.get(role)
        if cached is not None and not force_new:
            return cached
        with _provider_lock:
            cached = _text_providers_by_role.get(role)
            if cached is not None and not force_new:
                return cached
            cfg = _resolve_text_config()
            token = overrides.get("auth_token")
            merged = replace(
                cfg,
                model=overrides.get("model", cfg.model),
                base_url=overrides.get("base_url", cfg.base_url),
                # The role token authorizes the role's model. Set BOTH slots so it
                # works whether the provider client reads api_key (openai lane) or
                # auth_token (claude Bearer lane).
                api_key=token or cfg.api_key,
                auth_token=token or cfg.auth_token,
            )
            if not (merged.api_key or merged.auth_token):
                logger.warning("No credential configured for text provider (role=%s)", role)
                return None
            provider = _create_provider(merged)
            if not force_new:
                _text_providers_by_role[role] = provider
            logger.info("Text provider (role=%s) initialized: %s with model %s",
                        role, merged.provider_type, merged.model)
            return provider

    if _text_provider is not None and not force_new:
        return _text_provider

    with _provider_lock:
        if _text_provider is not None and not force_new:
            return _text_provider

        cfg = _resolve_text_config()
        if not (cfg.api_key or cfg.auth_token):
            logger.warning("No credential configured for text provider (API key or auth token)")
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
                   (use in background threads — see the agent runtime).

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

        provider = _create_provider(cfg, capability="image")
        if not force_new:
            _image_provider = provider
        logger.info(f"Image provider initialized: {cfg.provider_type} with model {cfg.model}")
        return provider


def _resolve_key_for(provider: str) -> Optional[str]:
    """Resolve the env API key for a text provider (used when a per-node model
    selection switches provider away from the configured default)."""
    if (provider or "").lower() == "claude":
        return _first(
            os.getenv("AI_TEXT_API_KEY"),
            os.getenv("ANTHROPIC_API_KEY"),
            os.getenv("CLAUDE_API_KEY"),
            os.getenv("AI_API_KEY"),
        )
    if (provider or "").lower() == "openai":
        # OpenAI-compatible chat gateway: the Bearer token is the api_key.
        return _first(
            os.getenv("AI_TEXT_API_KEY"),
            os.getenv("AI_TEXT_AUTH_TOKEN"),
            os.getenv("OPENAI_API_KEY"),
            os.getenv("AI_API_KEY"),
        )
    # gemini (and any other non-claude text provider) shares the gemini/AI chain.
    return _first(
        os.getenv("AI_TEXT_API_KEY"),
        os.getenv("AI_API_KEY"),
        os.getenv("GOOGLE_API_KEY"),
        os.getenv("GEMINI_API_KEY"),
    )


def _default_text_model_for(provider: str) -> str:
    if provider == "claude":
        return DEFAULT_CLAUDE_TEXT_MODEL
    if provider == "openai":
        return DEFAULT_OPENAI_TEXT_MODEL
    return DEFAULT_GEMINI_TEXT_MODEL


def build_text_provider(
    provider: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
) -> Optional[AIProvider]:
    """Build a one-off TEXT provider with per-call overrides (NOT cached).

    Used by the remix canvas so each agent node can pick its own model/provider.
    Image-only providers fall back to gemini. The API key is always resolved
    server-side from the environment — it is never accepted from the caller.
    Returns None when no key is configured for the resolved provider.
    """
    base = _resolve_text_config()
    provider_type = (provider or base.provider_type).lower()
    if provider_type not in TEXT_PROVIDERS:
        provider_type = "gemini"
    same_as_default = provider_type == base.provider_type

    api_key = base.api_key if same_as_default else (_resolve_key_for(provider_type) or base.api_key)
    auth_token = (
        base.auth_token if same_as_default
        else (_resolve_text_auth_token() if provider_type == "claude" else None)
    )
    resolved_model = model or (base.model if same_as_default else _default_text_model_for(provider_type))
    resolved_base = base_url or (base.base_url if same_as_default else None)

    cfg = ResolvedProviderConfig(
        provider_type=provider_type,
        api_key=api_key,
        model=resolved_model,
        base_url=resolved_base,
        auth_token=auth_token,
    )
    if not (cfg.api_key or cfg.auth_token):
        logger.warning("No credential for per-node text provider: %s", provider_type)
        return None
    return _create_provider(cfg)


def reset_providers():
    """Reset cached providers (thread-safe, useful for testing or config changes)."""
    global _text_provider, _image_provider
    with _provider_lock:
        _text_provider = None
        _image_provider = None
        _text_providers_by_role.clear()
    logger.info("AI providers cache reset")
