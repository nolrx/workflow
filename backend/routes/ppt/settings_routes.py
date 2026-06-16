"""
PPT Settings routes - handles user PPT settings management
"""
import logging
from datetime import datetime

from flask import Blueprint, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from backend.extensions import db
from backend.models.ppt.settings import PPTSettings
from backend.utils.response import error_response, success_response

logger = logging.getLogger(__name__)

ppt_settings_bp = Blueprint("ppt_settings", __name__)


@ppt_settings_bp.route("/settings", methods=["GET"])
@jwt_required()
def get_settings():
    """
    GET /api/ppt/settings - Get user's PPT settings

    Returns:
        User's PPT generation settings
    """
    try:
        user_id = get_jwt_identity()
        settings = PPTSettings.get_or_create(user_id)

        return success_response(settings.to_dict(include_sensitive=True))

    except Exception as e:
        logger.error(f"get_settings failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_settings_bp.route("/settings", methods=["PUT"])
@jwt_required()
def update_settings():
    """
    PUT /api/ppt/settings - Update user's PPT settings

    Request body:
    {
        "ai_provider": "gemini|openai",
        "api_base_url": "https://api.example.com",
        "api_key": "your-api-key",
        "text_model": "model-name",
        "image_model": "model-name",
        "image_resolution": "1K|2K|4K",
        "image_aspect_ratio": "16:9|4:3|1:1",
        "max_description_workers": 1-10,
        "max_image_workers": 1-10,
        "output_language": "zh|en|ja|auto"
    }

    Returns:
        Updated settings
    """
    try:
        user_id = get_jwt_identity()
        data = request.get_json()

        if not data:
            return error_response("BAD_REQUEST", "Request body is required", 400)

        settings = PPTSettings.get_or_create(user_id)

        # Update AI provider configuration
        if "ai_provider" in data:
            provider = data["ai_provider"]
            if provider and provider not in ["gemini", "openai"]:
                return error_response("BAD_REQUEST", "AI provider must be 'gemini' or 'openai'", 400)
            settings.ai_provider = provider

        if "api_base_url" in data:
            value = data["api_base_url"]
            if value is None or (isinstance(value, str) and value.strip() == ""):
                settings.api_base_url = None
            else:
                settings.api_base_url = str(value).strip()

        if "api_key" in data:
            value = data["api_key"]
            if value is None or (isinstance(value, str) and value.strip() == ""):
                settings.api_key = None
            else:
                settings.api_key = str(value).strip()

        if "text_model" in data:
            value = data["text_model"]
            settings.text_model = str(value).strip() if value else None

        if "image_model" in data:
            value = data["image_model"]
            settings.image_model = str(value).strip() if value else None

        # Update image generation settings
        if "image_resolution" in data:
            resolution = data["image_resolution"]
            if resolution not in ["1K", "2K", "4K"]:
                return error_response("BAD_REQUEST", "Resolution must be 1K, 2K, or 4K", 400)
            settings.image_resolution = resolution

        if "image_aspect_ratio" in data:
            # Allow common aspect ratios
            settings.image_aspect_ratio = data["image_aspect_ratio"]

        # Update worker settings
        if "max_description_workers" in data:
            workers = int(data["max_description_workers"])
            if workers < 1 or workers > 10:
                return error_response("BAD_REQUEST", "max_description_workers must be between 1 and 10", 400)
            settings.max_description_workers = workers

        if "max_image_workers" in data:
            workers = int(data["max_image_workers"])
            if workers < 1 or workers > 10:
                return error_response("BAD_REQUEST", "max_image_workers must be between 1 and 10", 400)
            settings.max_image_workers = workers

        # Update output language
        if "output_language" in data:
            language = data["output_language"]
            if language not in ["zh", "en", "ja", "auto"]:
                return error_response("BAD_REQUEST", "Output language must be 'zh', 'en', 'ja', or 'auto'", 400)
            settings.output_language = language

        settings.updated_at = datetime.utcnow()
        db.session.commit()

        logger.info(f"Settings updated for user {user_id}")
        return success_response(settings.to_dict(include_sensitive=True), "Settings updated successfully")

    except ValueError as e:
        return error_response("BAD_REQUEST", str(e), 400)
    except Exception as e:
        db.session.rollback()
        logger.error(f"update_settings failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_settings_bp.route("/settings/reset", methods=["POST"])
@jwt_required()
def reset_settings():
    """
    POST /api/ppt/settings/reset - Reset settings to default values

    Returns:
        Reset settings
    """
    try:
        user_id = get_jwt_identity()
        settings = PPTSettings.get_or_create(user_id)

        # Reset to defaults
        settings.ai_provider = None
        settings.api_base_url = None
        settings.api_key = None
        settings.text_model = None
        settings.image_model = None
        settings.image_resolution = "2K"
        settings.image_aspect_ratio = "16:9"
        settings.max_description_workers = 3
        settings.max_image_workers = 2
        settings.output_language = "zh"
        settings.updated_at = datetime.utcnow()

        db.session.commit()

        logger.info(f"Settings reset to defaults for user {user_id}")
        return success_response(settings.to_dict(include_sensitive=True), "Settings reset to defaults")

    except Exception as e:
        db.session.rollback()
        logger.error(f"reset_settings failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_settings_bp.route("/settings/validate", methods=["POST"])
@jwt_required()
def validate_api_settings():
    """
    POST /api/ppt/settings/validate - Validate API settings by making a test call

    Returns:
        Validation result
    """
    try:
        user_id = get_jwt_identity()
        settings = PPTSettings.get_or_create(user_id)

        # Check if API is configured
        if not settings.api_key:
            return success_response({
                "valid": False,
                "message": "API key not configured",
                "using_default": True
            })

        # Try to initialize provider with user settings
        try:
            from backend.services.ai.factory import get_text_provider

            # Get provider (will use default config if user settings not available)
            provider = get_text_provider()

            if provider and provider.is_configured():
                return success_response({
                    "valid": True,
                    "message": "API settings are valid",
                    "provider": settings.ai_provider or "default"
                })
            else:
                return success_response({
                    "valid": False,
                    "message": "Provider not properly configured",
                    "using_default": True
                })

        except Exception as e:
            return success_response({
                "valid": False,
                "message": f"Validation failed: {str(e)}",
                "using_default": True
            })

    except Exception as e:
        logger.error(f"validate_api_settings failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_settings_bp.route("/output-language", methods=["GET"])
@jwt_required()
def get_output_language():
    """
    GET /api/ppt/output-language - Get user's output language setting

    Returns:
        Output language
    """
    try:
        user_id = get_jwt_identity()
        settings = PPTSettings.get_or_create(user_id)

        return success_response({
            "output_language": settings.output_language
        })

    except Exception as e:
        logger.error(f"get_output_language failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)


@ppt_settings_bp.route("/output-language", methods=["PUT"])
@jwt_required()
def set_output_language():
    """
    PUT /api/ppt/output-language - Set user's output language

    Request body:
    {
        "output_language": "zh|en|ja|auto"
    }

    Returns:
        Updated output language
    """
    try:
        user_id = get_jwt_identity()
        data = request.get_json() or {}

        language = data.get("output_language")
        if language not in ["zh", "en", "ja", "auto"]:
            return error_response("BAD_REQUEST", "Output language must be 'zh', 'en', 'ja', or 'auto'", 400)

        settings = PPTSettings.get_or_create(user_id)
        settings.output_language = language
        settings.updated_at = datetime.utcnow()
        db.session.commit()

        return success_response({
            "output_language": settings.output_language
        })

    except Exception as e:
        db.session.rollback()
        logger.error(f"set_output_language failed: {str(e)}", exc_info=True)
        return error_response("SERVER_ERROR", str(e), 500)
