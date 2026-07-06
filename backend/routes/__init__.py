"""
Routes package
"""
from backend.routes.auth_routes import auth_bp
from backend.routes.credit_routes import credit_bp
from backend.routes.team_routes import team_bp
from backend.routes.user_routes import user_bp

__all__ = ["auth_bp", "user_bp", "team_bp", "credit_bp"]
