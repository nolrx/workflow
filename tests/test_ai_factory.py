"""
Tests for the capability-based AI provider routing (the "判定").

These tests do not hit the network — they verify that text generation routes to
the text provider and image generation routes to the image provider, and that
each provider refuses the other capability.
"""
import pytest

from backend.services.ai import (
    get_image_provider,
    get_provider_config,
    get_text_provider,
    reset_providers,
)


@pytest.fixture
def claude_text_env(monkeypatch):
    monkeypatch.setenv("AI_TEXT_PROVIDER", "claude")
    monkeypatch.setenv("AI_TEXT_MODEL", "claude-opus-4-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    reset_providers()


@pytest.fixture
def panlaxy_image_env(monkeypatch):
    # Clear the capability-level image overrides first: they take precedence over
    # the PANLAXY_* keys in the resolver, so if the ambient .env sets any of them
    # (e.g. AI_IMAGE_MODEL) they would leak in and defeat this fixture's intent of
    # exercising panlaxy-specific resolution in isolation.
    monkeypatch.delenv("AI_IMAGE_MODEL", raising=False)
    monkeypatch.delenv("AI_IMAGE_API_KEY", raising=False)
    monkeypatch.delenv("AI_IMAGE_BASE_URL", raising=False)
    monkeypatch.setenv("AI_IMAGE_PROVIDER", "panlaxy")
    monkeypatch.setenv("PANLAXY_API_KEY", "sk-panlaxy-test-key")
    monkeypatch.setenv("PANLAXY_BASE_URL", "https://api.panlaxy.io/v1")
    monkeypatch.setenv("PANLAXY_IMAGE_MODEL", "gpt-image-1")
    reset_providers()


@pytest.fixture
def gemini_image_env(monkeypatch):
    # Image provider = gemini with NO explicit AI_IMAGE_MODEL. It must fall back to
    # a NATIVE gemini image model, never a panlaxy id (gpt-image-2): the gemini
    # provider calls generateContent + response_modalities, so a wrong/text model
    # returns text only (finish_reason=STOP, no image).
    monkeypatch.delenv("AI_IMAGE_MODEL", raising=False)
    monkeypatch.delenv("AI_IMAGE_API_KEY", raising=False)
    monkeypatch.delenv("AI_IMAGE_BASE_URL", raising=False)
    monkeypatch.setenv("AI_IMAGE_PROVIDER", "gemini")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google-key")
    reset_providers()


@pytest.fixture
def openai_image_env(monkeypatch):
    # Image provider = the real OpenAI (ChatGPT) image API. With no overrides it
    # must resolve to gpt-image-2 at OpenAI's own base URL, using OPENAI_API_KEY.
    monkeypatch.delenv("AI_IMAGE_MODEL", raising=False)
    monkeypatch.delenv("AI_IMAGE_API_KEY", raising=False)
    monkeypatch.delenv("AI_IMAGE_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_IMAGE_MODEL", raising=False)
    monkeypatch.setenv("AI_IMAGE_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    reset_providers()


def test_text_provider_is_claude(claude_text_env):
    provider = get_text_provider()
    assert provider is not None
    assert provider.provider_name == "claude"
    assert provider.model == "claude-opus-4-8"


def test_image_provider_is_panlaxy(panlaxy_image_env):
    provider = get_image_provider()
    assert provider is not None
    assert provider.provider_name == "panlaxy"
    assert provider.model == "gpt-image-1"
    assert provider.base_url == "https://api.panlaxy.io/v1"


def test_image_provider_is_openai(openai_image_env):
    """gpt-image-2 routes through the real OpenAI image API (not Panlaxy)."""
    provider = get_image_provider()
    assert provider is not None
    assert provider.provider_name == "openai"
    assert provider.model == "gpt-image-2"
    assert provider.base_url == "https://api.openai.com/v1"


def test_gemini_image_default_is_a_native_image_model(gemini_image_env):
    """An unset AI_IMAGE_MODEL on the gemini image provider must resolve to a
    native gemini image model — never a panlaxy id like gpt-image-2, which makes
    generateContent return text-only (finish_reason=STOP, no image)."""
    provider = get_image_provider()
    assert provider is not None
    assert provider.provider_name == "gemini"
    assert provider.model != "gpt-image-2"
    assert provider.model.startswith("gemini-")
    assert "image" in provider.model


def test_text_and_image_use_different_providers(claude_text_env, panlaxy_image_env):
    text = get_text_provider()
    image = get_image_provider()
    assert text.provider_name == "claude"
    assert image.provider_name == "panlaxy"
    assert text is not image


def test_claude_refuses_image_generation(claude_text_env):
    """Text provider must not produce images — that's the image API's job."""
    provider = get_text_provider()
    result = provider.generate_image("a cat")
    assert result.success is False
    assert result.image_data is None
    assert "image" in result.error.lower()


def test_panlaxy_refuses_text_generation(panlaxy_image_env):
    """Image provider must not produce text — that's the text API's job."""
    provider = get_image_provider()
    result = provider.generate_text("hello")
    assert result.success is False
    assert result.text == ""
    assert "text" in result.error.lower()


def test_provider_config_resolution(claude_text_env, panlaxy_image_env):
    cfg = get_provider_config()
    assert cfg.text.provider_type == "claude"
    assert cfg.text.model == "claude-opus-4-8"
    assert cfg.image.provider_type == "panlaxy"
    assert cfg.image.model == "gpt-image-1"


def test_force_new_does_not_pollute_cache(claude_text_env):
    cached = get_text_provider()
    fresh = get_text_provider(force_new=True)
    assert fresh is not cached
    # Cached instance must be unchanged after a force_new call.
    assert get_text_provider() is cached


def test_missing_key_returns_none(monkeypatch):
    monkeypatch.setenv("AI_TEXT_PROVIDER", "claude")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
    monkeypatch.delenv("AI_TEXT_API_KEY", raising=False)
    monkeypatch.delenv("AI_API_KEY", raising=False)
    # A Bearer auth token (gateway) is also a credential — clear it too, else a
    # token in the real .env (loaded by config) would make the provider non-None.
    monkeypatch.delenv("AI_TEXT_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    reset_providers()
    assert get_text_provider() is None


# --- role-based text model tiers (reasoning vs fast) --------------------------
@pytest.fixture
def role_env(monkeypatch):
    """openai-lane gateway with distinct per-role models + tokens (as in prod:
    a token is authorized per-model on the gateway)."""
    monkeypatch.setenv("AI_TEXT_PROVIDER", "openai")
    monkeypatch.setenv("AI_TEXT_MODEL", "flash-model")
    monkeypatch.setenv("AI_TEXT_AUTH_TOKEN", "flash-token")
    monkeypatch.setenv("AI_TEXT_BASE_URL", "https://gw/v1")
    monkeypatch.setenv("AI_TEXT_REASONING_MODEL", "pro-model")
    monkeypatch.setenv("AI_TEXT_REASONING_AUTH_TOKEN", "pro-token")
    monkeypatch.delenv("AI_TEXT_FAST_MODEL", raising=False)
    monkeypatch.delenv("AI_TEXT_FAST_AUTH_TOKEN", raising=False)
    reset_providers()


def test_text_role_config_resolves_overrides(role_env):
    from backend.services.ai.factory import text_model_for, text_role_config

    assert text_role_config("reasoning") == {"model": "pro-model", "auth_token": "pro-token"}
    # fast falls back to the default AI_TEXT_MODEL (no fast-specific env set).
    assert text_role_config("fast") == {}
    assert text_role_config(None) == {}
    assert text_model_for("reasoning") == "pro-model"
    assert text_model_for("fast") == "flash-model"  # default fallback


def test_role_provider_uses_role_model_and_token(role_env):
    reasoning = get_text_provider(role="reasoning", force_new=True)
    fast = get_text_provider(role="fast", force_new=True)
    default = get_text_provider(force_new=True)
    # Reasoning routes to the pro model + its OWN token (per-model gateway auth).
    assert reasoning.model == "pro-model"
    assert reasoning.api_key == "pro-token"
    # Fast (no fast-specific env) inherits the default model + token.
    assert fast.model == "flash-model"
    assert fast.api_key == "flash-token"
    assert default.model == "flash-model"


def test_role_provider_cached_per_role(role_env):
    a = get_text_provider(role="reasoning")
    b = get_text_provider(role="reasoning")
    assert a is b  # per-role cache hit
    # A different role is a distinct instance.
    assert get_text_provider(role="fast") is not a
