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


def create_app(config_name: str = None, *, reconcile_on_boot: bool = False) -> Flask:
    """Application factory for creating Flask app instances.

    ``reconcile_on_boot`` MUST stay False for every incidental ``create_app()``
    (tests, scripts, an ops one-liner that just needs an app context). It is True
    only on the real server entrypoints (``serve()`` for gunicorn, ``__main__``
    for dev). Reconciliation re-dispatches EVERY in-flight run platform-wide and
    spawns their sandbox containers — making that a side effect of merely building
    an app turns any `create_app('production')` against the prod DB into an
    accidental mass-resume storm. Keep it opt-in.
    """
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
    from backend.routes.code import (
        app_proxy_bp,
        code_preview_bp,
        code_project_bp,
        figma_bp,
        fullstack_bp,
        github_bp,
    )
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
    # Full-stack pipeline orchestration (start the 3 concurrent runs, deploy,
    # status, contract). Mounted under /api/code alongside the project routes.
    app.register_blueprint(fullstack_bp, url_prefix="/api/code")
    # Session-bound deployed preview of generated frontend projects. Mounted at the
    # top level (not under /api) so it reads like a real deployment; nginx proxies
    # the /preview prefix to the backend (see frontend/nginx/default.conf).
    app.register_blueprint(code_preview_bp, url_prefix="/preview")
    # Reverse proxy from the served frontend to the live generated backend
    # container (/app/<pid>/api/... -> http://<container>:<port>/...). Top-level
    # like /preview; nginx proxies the /app prefix to the backend.
    app.register_blueprint(app_proxy_bp, url_prefix="/app")
    app.register_blueprint(admin_bp, url_prefix="/api/admin")

    # Health checks — liveness vs readiness.
    # Liveness: the process is up and can serve. Used by the Docker HEALTHCHECK and
    # external probes; stays 200 even while draining (the process is still alive).
    @app.route("/health")
    @app.route("/health/live")
    def health():
        return jsonify({"status": "healthy", "service": "ai-creative-studio"})

    # Readiness: is this instance ready to take NEW work? Returns 503 while draining
    # for a graceful redeploy, so the deploy script (and any future load balancer)
    # stops routing to / waiting on the instance that is shutting down. See
    # backend/services/lifecycle.py and scripts/deploy-backend.sh.
    @app.route("/health/ready")
    def health_ready():
        from backend.services.lifecycle import is_draining

        if is_draining():
            return jsonify({"status": "draining", "ready": False}), 503
        return jsonify({"status": "ready", "ready": True})

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

    # Reconcile runs orphaned by a previous process (e.g. a restart / crash): the
    # background executor is in-process, so a replaced process leaves in-flight
    # runs stuck 'running' forever (and, counting as ACTIVE, they block the user
    # from re-running). Resume (or fail+refund past the budget) on boot. Fails
    # soft. ONLY on the real server boot — never as a side effect of an incidental
    # create_app() (see the factory docstring): this re-dispatches every run and
    # spawns containers, so an ad-hoc create_app('production') would storm prod.
    if reconcile_on_boot:
        try:
            from backend.services.agent.runtime import reconcile_orphaned_runs

            reconcile_orphaned_runs(app)
        except Exception as error:  # noqa: BLE001 — never block startup on reconciliation
            logger.warning("Orphaned-run reconciliation skipped: %s", error)

    # Seed editable system prompts into MongoDB (idempotent; only inserts
    # missing keys). Fails soft — if Mongo is unreachable the app still runs off
    # the bundled default prompts.
    try:
        from backend.services.prompts import prompt_store

        prompt_store.seed_defaults()
    except Exception as error:  # noqa: BLE001 — never block startup on prompt seeding
        logger.warning("Prompt seeding skipped: %s", error)

    return app


def serve() -> Flask:
    """Real server entrypoint (gunicorn factory target).

    The ONLY app build that reconciles orphaned runs on boot. Gunicorn loads
    ``backend.app:serve()``; every other code path uses ``create_app()`` and is
    side-effect-free.
    """
    return create_app(reconcile_on_boot=True)


if __name__ == "__main__":
    app = serve()
    app.run(host="0.0.0.0", port=5001, debug=True)
