"""
PPT Studio models
"""
from backend.models.ppt.project import PPTProject
from backend.models.ppt.page import PPTPage
from backend.models.ppt.page_image_version import PPTPageImageVersion
from backend.models.ppt.task import PPTTask
from backend.models.ppt.material import PPTMaterial
from backend.models.ppt.reference_file import PPTReferenceFile
from backend.models.ppt.user_template import PPTUserTemplate
from backend.models.ppt.settings import PPTSettings

__all__ = [
    "PPTProject",
    "PPTPage",
    "PPTPageImageVersion",
    "PPTTask",
    "PPTMaterial",
    "PPTReferenceFile",
    "PPTUserTemplate",
    "PPTSettings",
]
