"""
PPT Studio services
"""
from backend.services.ppt.editable_export_service import (
    EditableExportService,
    ExportResult,
    TextElement,
    create_editable_pptx,
)
from backend.services.ppt.export_service import (
    create_simple_pptx,
    export_project_to_pptx,
)
from backend.services.ppt.file_service import PPTFileService, get_ai_provider_for_user
from backend.services.ppt.generation_service import (
    build_outline_text_from_pages,
    extract_and_clean_description,
    extract_image_urls_from_markdown,
    flatten_outline_to_pages,
    generate_outline_from_idea,
    generate_page_description,
    generate_page_image,
    is_mineru_image_path,
    normalize_image_path,
    parse_outline_text,
    remove_markdown_images,
)
from backend.services.ppt.prompts import (
    LANGUAGE_CONFIG,
    get_batch_text_attribute_extraction_prompt,
    get_clean_background_prompt,
    get_descriptions_refinement_prompt,
    get_image_edit_prompt,
    get_image_edit_with_region_prompt,
    get_image_generation_prompt,
    get_language_instruction,
    get_outline_generation_prompt,
    get_outline_parsing_prompt,
    get_outline_refinement_prompt,
    get_page_description_prompt,
    get_ppt_language_instruction,
    get_quality_enhancement_prompt,
    get_text_attribute_extraction_prompt,
)
from backend.services.ppt.task_manager import ppt_task_manager

__all__ = [
    # Editable export
    "EditableExportService",
    "ExportResult",
    "TextElement",
    "create_editable_pptx",
    # Export services
    "create_simple_pptx",
    "export_project_to_pptx",
    # File service
    "PPTFileService",
    "get_ai_provider_for_user",
    # Generation utilities
    "build_outline_text_from_pages",
    "extract_and_clean_description",
    "extract_image_urls_from_markdown",
    "flatten_outline_to_pages",
    "generate_outline_from_idea",
    "generate_page_description",
    "generate_page_image",
    "is_mineru_image_path",
    "normalize_image_path",
    "parse_outline_text",
    "remove_markdown_images",
    # Prompts
    "LANGUAGE_CONFIG",
    "get_batch_text_attribute_extraction_prompt",
    "get_clean_background_prompt",
    "get_descriptions_refinement_prompt",
    "get_image_edit_prompt",
    "get_image_edit_with_region_prompt",
    "get_image_generation_prompt",
    "get_language_instruction",
    "get_outline_generation_prompt",
    "get_outline_parsing_prompt",
    "get_outline_refinement_prompt",
    "get_page_description_prompt",
    "get_ppt_language_instruction",
    "get_quality_enhancement_prompt",
    "get_text_attribute_extraction_prompt",
    # Task manager
    "ppt_task_manager",
]
