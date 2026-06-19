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
    reset_providers()
    assert get_text_provider() is None
