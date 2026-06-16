"""
PPT Studio routes
"""
from backend.routes.ppt.project_routes import ppt_project_bp
from backend.routes.ppt.page_routes import ppt_page_bp
from backend.routes.ppt.file_routes import ppt_file_bp
from backend.routes.ppt.export_routes import ppt_export_bp
from backend.routes.ppt.material_routes import ppt_material_bp
from backend.routes.ppt.reference_file_routes import ppt_reference_file_bp
from backend.routes.ppt.template_routes import ppt_template_bp
from backend.routes.ppt.settings_routes import ppt_settings_bp

__all__ = [
    "ppt_project_bp",
    "ppt_page_bp",
    "ppt_file_bp",
    "ppt_export_bp",
    "ppt_material_bp",
    "ppt_reference_file_bp",
    "ppt_template_bp",
    "ppt_settings_bp",
]
