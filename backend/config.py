"""
Configuration settings for AI Creative Studio
"""

import os
from datetime import timedelta
from pathlib import Path

# Load .env file from project root
from dotenv import load_dotenv

env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)


class BaseConfig:
    """Base configuration."""

    # Security: Keys must be set via environment variables
    # Development uses auto-generated keys if not set, production requires explicit config
    SECRET_KEY = os.getenv("SECRET_KEY")
    JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(minutes=30)
    JWT_REFRESH_TOKEN_EXPIRES = timedelta(days=30)
    # Accept the JWT from the Authorization header (the default, used by every
    # normal request via the axios interceptor) OR from a JSON body key. The web
    # client posts the refresh token in the request body ({"refresh_token": ...})
    # with no Authorization header, so /auth/refresh's @jwt_required(refresh=True)
    # MUST also look in the body — otherwise every refresh 401s and users get
    # bounced to /login the moment their 30-min access token lapses (and the SSE
    # auto-reconnect turns that into a 401 storm). Headers are tried first, so
    # ordinary header-bearing requests never parse the body (uploads stay safe);
    # the default JSON keys ("access_token" / "refresh_token") already match what
    # the client sends.
    JWT_TOKEN_LOCATION = ["headers", "json"]

    # SQLAlchemy Configuration
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
    }

    # File Upload Configuration
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50MB
    UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "uploads")

    # AI Provider Configuration (unified with backend.services.ai.factory)
    # Capability-based routing: text generation and image generation are
    # configured independently. AI_PROVIDER is the fallback for both.
    AI_PROVIDER = os.getenv("AI_PROVIDER", "gemini")

    # Text generation (default: Claude)
    AI_TEXT_PROVIDER = os.getenv("AI_TEXT_PROVIDER", AI_PROVIDER)
    AI_TEXT_MODEL = os.getenv("AI_TEXT_MODEL", "claude-opus-4-8")
    AI_TEXT_MAX_TOKENS = int(os.getenv("AI_TEXT_MAX_TOKENS", "32000"))

    # Image generation. Default model is a NATIVE gemini image model (not imagen-*,
    # which uses a different predict API). The factory (services/ai/factory.py) is
    # the authoritative resolver; this mirrors its default for consistency.
    AI_IMAGE_PROVIDER = os.getenv("AI_IMAGE_PROVIDER", AI_PROVIDER)
    AI_IMAGE_MODEL = os.getenv("AI_IMAGE_MODEL", "gemini-3.1-flash-image")

    AI_BASE_URL = os.getenv("AI_BASE_URL")

    # API Keys (AI_API_KEY is primary, with fallbacks)
    AI_API_KEY = (
        os.getenv("AI_API_KEY") or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    )
    # Claude (text) API key
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY")
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    # Panlaxy (image) API — OpenAI-compatible
    PANLAXY_BASE_URL = os.getenv("PANLAXY_BASE_URL", "https://api.panlaxy.io/v1")
    PANLAXY_API_KEY = os.getenv("PANLAXY_API_KEY")
    PANLAXY_IMAGE_MODEL = os.getenv("PANLAXY_IMAGE_MODEL", "gpt-image-2")
    PANLAXY_IMAGE_QUALITY = os.getenv("PANLAXY_IMAGE_QUALITY", "medium")
    PANLAXY_IMAGE_SIZE = os.getenv("PANLAXY_IMAGE_SIZE", "1024x1024")

    # Stripe Configuration
    STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
    STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")

    # GitHub integration (org-level GitHub App). When unset, the integration is
    # disabled and the auto-sync hook is a silent no-op. The App must be granted
    # "Administration: write" (to create repos) + "Contents: write" (to push).
    # The auth/sync layers read these from os.getenv directly so they work inside
    # background workflow threads; mirrored here for visibility.
    GITHUB_APP_ID = os.getenv("GITHUB_APP_ID")
    # PEM private key, either inline (literal "\n" escapes are normalised) or via path.
    GITHUB_APP_PRIVATE_KEY = os.getenv("GITHUB_APP_PRIVATE_KEY")
    GITHUB_APP_PRIVATE_KEY_PATH = os.getenv("GITHUB_APP_PRIVATE_KEY_PATH")
    # Optional: pin the installation; otherwise resolved from GET /app/installations.
    GITHUB_APP_INSTALLATION_ID = os.getenv("GITHUB_APP_INSTALLATION_ID")
    # Target org/user to create repos under; defaults to the installation account.
    GITHUB_REPO_OWNER = os.getenv("GITHUB_REPO_OWNER")
    GITHUB_API_BASE = os.getenv("GITHUB_API_BASE", "https://api.github.com")
    GITHUB_REPO_VISIBILITY = os.getenv("GITHUB_REPO_VISIBILITY", "private")
    GITHUB_REPO_PREFIX = os.getenv("GITHUB_REPO_PREFIX", "")
    GITHUB_PUSH_DIST = os.getenv("GITHUB_PUSH_DIST", "true").lower() in ("1", "true", "yes")

    # Redis Configuration
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # MongoDB Configuration (stores editable system prompts). The accessor in
    # backend.services.mongo reads these from os.getenv directly so it works in
    # background threads; mirrored here for visibility. Optional — the app falls
    # back to bundled default prompts when Mongo is unreachable.
    MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
    MONGODB_DB = os.getenv("MONGODB_DB", "ai_creative_studio")


class DevelopmentConfig(BaseConfig):
    """Development configuration."""

    DEBUG = True
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///ai_creative_studio.db")

    # Auto-generate keys for development if not set
    SECRET_KEY = os.getenv("SECRET_KEY") or os.urandom(32).hex()
    JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY") or os.urandom(32).hex()


class ProductionConfig(BaseConfig):
    """Production configuration."""

    DEBUG = False
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL")

    # Production requires explicit security keys - will raise error if not set
    SECRET_KEY = os.getenv("SECRET_KEY")
    JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY")

    def __init__(self):
        # Validate required environment variables in production
        if not self.SECRET_KEY:
            raise ValueError("SECRET_KEY environment variable is required in production")
        if not self.JWT_SECRET_KEY:
            raise ValueError("JWT_SECRET_KEY environment variable is required in production")
        if not self.SQLALCHEMY_DATABASE_URI:
            raise ValueError("DATABASE_URL environment variable is required in production")

    # Override with production-specific settings
    SQLALCHEMY_ENGINE_OPTIONS = {
        **BaseConfig.SQLALCHEMY_ENGINE_OPTIONS,
        "pool_size": 10,
        "max_overflow": 20,
    }


class TestingConfig(BaseConfig):
    """Testing configuration."""

    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(minutes=5)
