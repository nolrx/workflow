"""
Code creation routes.
"""
from backend.routes.code.figma_routes import figma_bp
from backend.routes.code.github_routes import github_bp
from backend.routes.code.project_routes import code_project_bp

__all__ = ["code_project_bp", "figma_bp", "github_bp"]
