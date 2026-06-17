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

    # Image generation (default: Panlaxy)
    AI_IMAGE_PROVIDER = os.getenv("AI_IMAGE_PROVIDER", AI_PROVIDER)
    AI_IMAGE_MODEL = os.getenv("AI_IMAGE_MODEL", "imagen-3.0-generate-002")

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

    # Redis Configuration
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")


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
