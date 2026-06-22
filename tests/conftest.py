"""
Shared pytest fixtures for the AI provider test suite.

Loads the project `.env` so providers can resolve API keys, and resets the
cached AI providers around every test so configuration changes (real or
monkeypatched) take effect.
"""
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

# Load the project .env so AI_* settings and API keys are available.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Force MongoDB to be treated as unavailable during tests so the prompt store
# always resolves to the bundled defaults — deterministic regardless of whether
# a developer happens to have a real Mongo running locally (which could hold
# edited prompts). Set before importing the prompt store so the first probe uses
# this. Port 1 refuses fast; the probe result is cached unavailable for the run.
os.environ["MONGODB_URI"] = "mongodb://localhost:1"

from backend.services.ai import reset_providers  # noqa: E402
from backend.services.mongo import reset_mongo  # noqa: E402
from backend.services.prompts import prompt_store  # noqa: E402

reset_mongo()


@pytest.fixture(autouse=True)
def _reset_ai_providers():
    """Ensure each test sees a fresh provider cache."""
    reset_providers()
    prompt_store.reset_cache()
    yield
    reset_providers()
    prompt_store.reset_cache()
