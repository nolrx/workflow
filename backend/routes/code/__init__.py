"""
Code creation routes.
"""
from backend.routes.code.figma_routes import figma_bp
from backend.routes.code.fullstack_routes import app_proxy_bp, fullstack_bp
from backend.routes.code.github_routes import github_bp
from backend.routes.code.preview_routes import code_preview_bp
from backend.routes.code.project_routes import code_project_bp

__all__ = [
    "code_preview_bp",
    "code_project_bp",
    "figma_bp",
    "github_bp",
    "fullstack_bp",
    "app_proxy_bp",
]
