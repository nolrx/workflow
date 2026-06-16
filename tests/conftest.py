"""
Shared pytest fixtures for the AI provider test suite.

Loads the project `.env` so providers can resolve API keys, and resets the
cached AI providers around every test so configuration changes (real or
monkeypatched) take effect.
"""
from pathlib import Path

import pytest
from dotenv import load_dotenv

# Load the project .env so AI_* settings and API keys are available.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from backend.services.ai import reset_providers  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_ai_providers():
    """Ensure each test sees a fresh provider cache."""
    reset_providers()
    yield
    reset_providers()
