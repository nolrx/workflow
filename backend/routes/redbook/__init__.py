"""
RedBook routes
"""
from backend.routes.redbook.content_routes import redbook_content_bp
from backend.routes.redbook.image_routes import redbook_image_bp
from backend.routes.redbook.outline_routes import redbook_outline_bp
from backend.routes.redbook.task_routes import redbook_task_bp

__all__ = [
    "redbook_task_bp",
    "redbook_outline_bp",
    "redbook_image_bp",
    "redbook_content_bp",
]
