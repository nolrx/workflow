"""
AI Creative Studio - Flask Application Factory
"""

import logging
import os

from flask import Flask, jsonify
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

from backend.extensions import db, jwt

logger = logging.getLogger(__name__)


def create_app(config_name: str = None) -> Flask:
    """Application factory for creating Flask app instances."""
    app = Flask(__name__)

    # Load configuration
    config_name = config_name or os.getenv("FLASK_ENV", "development")
    app.config.from_object(f"backend.config.{config_name.capitalize()}Config")

    # Initialize extensions
    db.init_app(app)
    jwt.init_app(app)

    # CORS: Use whitelist from environment variable, default to localhost for development
    allowed_origins = os.getenv(
        "CORS_ORIGINS", "http://localhost:3000,http://localhost:5173"
    ).split(",")
    CORS(app, resources={r"/api/*": {"origins": allowed_origins}})

    # Set up request logging middleware
    from backend.middleware import setup_request_logging

    setup_request_logging(app)

    # Register blueprints
    from backend.routes.admin_routes import admin_bp
    from backend.routes.agent_routes import agent_bp
    from backend.routes.auth_routes import auth_bp
    from backend.routes.code import code_project_bp, figma_bp, github_bp
    from backend.routes.credit_routes import credit_bp
    from backend.routes.team_routes import team_bp
    from backend.routes.user_routes import user_bp

    app.register_blueprint(auth_bp, url_prefix="/api/auth")
    app.register_blueprint(user_bp, url_prefix="/api/users")
    app.register_blueprint(team_bp, url_prefix="/api/teams")
    app.register_blueprint(credit_bp, url_prefix="/api/credits")
    app.register_blueprint(agent_bp, url_prefix="/api/agent")
    app.register_blueprint(code_project_bp, url_prefix="/api/code")
    app.register_blueprint(figma_bp, url_prefix="/api/code/figma")
    app.register_blueprint(github_bp, url_prefix="/api/code/github")
    app.register_blueprint(admin_bp, url_prefix="/api/admin")

    # Health check endpoint
    @app.route("/health")
    def health():
        return jsonify({"status": "healthy", "service": "ai-creative-studio"})

    @app.route("/")
    def index():
        return jsonify(
            {
                "name": "AI Creative Studio API",
                "version": "1.0.0",
                "endpoints": {
                    "auth": "/api/auth",
                    "users": "/api/users",
                    "teams": "/api/teams",
                    "credits": "/api/credits",
                    "agent": "/api/agent",
                    "code": "/api/code",
                    "admin": "/api/admin",
                },
            }
        )

    # Global error handlers
    @app.errorhandler(HTTPException)
    def handle_http_exception(e):
        """Handle HTTP exceptions with consistent JSON response"""
        return jsonify(
            {"success": False, "error": e.name.upper().replace(" ", "_"), "message": e.description}
        ), e.code

    @app.errorhandler(Exception)
    def handle_exception(e):
        """Handle all other exceptions"""
        logger.error(f"Unhandled exception: {e}", exc_info=True)
        # Don't expose internal error details in production
        if app.debug:
            message = str(e)
        else:
            message = "An internal server error occurred"
        return jsonify({"success": False, "error": "SERVER_ERROR", "message": message}), 500

    @app.errorhandler(404)
    def not_found(e):
        """Handle 404 errors"""
        return jsonify(
            {
                "success": False,
                "error": "NOT_FOUND",
                "message": "The requested resource was not found",
            }
        ), 404

    @app.errorhandler(500)
    def server_error(e):
        """Handle 500 errors"""
        logger.error(f"Server error: {e}", exc_info=True)
        return jsonify(
            {
                "success": False,
                "error": "SERVER_ERROR",
                "message": "An internal server error occurred",
            }
        ), 500

    # Create database tables
    with app.app_context():
        db.create_all()

    # Seed editable system prompts into MongoDB (idempotent; only inserts
    # missing keys). Fails soft — if Mongo is unreachable the app still runs off
    # the bundled default prompts.
    try:
        from backend.services.prompts import prompt_store

        prompt_store.seed_defaults()
    except Exception as error:  # noqa: BLE001 — never block startup on prompt seeding
        logger.warning("Prompt seeding skipped: %s", error)

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=5001, debug=True)
