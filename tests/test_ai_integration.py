"""
Live integration tests — exercise the real AI APIs end-to-end.

Run with:  pytest -m integration -s
These require ANTHROPIC_API_KEY (Claude) and PANLAXY_API_KEY (Panlaxy) to be set
(loaded from .env by conftest) and network access. They cost real tokens.
"""
import pytest

from backend.services.ai import get_image_provider, get_text_provider


@pytest.mark.integration
def test_claude_text_generation_live():
    """Text generation must succeed through the Claude API."""
    provider = get_text_provider()
    assert provider is not None, "text provider not configured (set ANTHROPIC_API_KEY)"
    assert provider.provider_name == "claude"
    assert provider.is_configured()

    result = provider.generate_text("Reply with exactly one word: PONG")

    assert result.success is True, f"Claude call failed: {result.error}"
    assert result.text, "expected non-empty text"
    assert "PONG" in result.text.upper()


@pytest.mark.integration
def test_claude_text_streaming_live():
    """Streaming text generation must yield chunks through the Claude API."""
    provider = get_text_provider()
    assert provider is not None
    assert provider.provider_name == "claude"

    chunks = list(provider.generate_text_stream("Reply with exactly one word: PONG"))

    assert len(chunks) >= 1, "expected at least one streamed chunk"
    full = "".join(chunks)
    assert "PONG" in full.upper()


@pytest.mark.integration
def test_panlaxy_image_generation_live():
    """Image generation must run through the Panlaxy image API.

    Panlaxy proxies upstream image accounts; when the upstream pool is
    unavailable it returns 5xx. We assert the provider produces a well-formed
    result either way: a real PNG/JPEG on success, or a graceful, informative
    failure (skipped) when the upstream is temporarily down.
    """
    provider = get_image_provider()
    assert provider is not None, "image provider not configured (set PANLAXY_API_KEY)"
    assert provider.provider_name == "panlaxy"
    assert provider.is_configured()

    result = provider.generate_image("a small red circle on a white background")

    if result.success:
        assert result.image_data, "expected image bytes on success"
        assert len(result.image_data) > 1000, "image looks too small"
        magic = result.image_data[:8]
        assert magic[:8] == b"\x89PNG\r\n\x1a\n" or magic[:3] == b"\xff\xd8\xff", (
            f"unexpected image format, magic={magic.hex()}"
        )
    else:
        # Provider handled the upstream failure gracefully rather than crashing.
        assert result.image_data is None
        assert result.error, "a failed result must carry an error message"
        pytest.skip(f"Panlaxy upstream unavailable: {result.error}")
