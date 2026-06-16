"""
PPT Generation Service - handles AI-powered content generation
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from backend.services.ppt.prompts import (
    get_image_generation_prompt,
    get_outline_generation_prompt,
    get_outline_parsing_prompt,
    get_page_description_prompt,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Markdown Image Extraction Utilities
# =============================================================================

def extract_image_urls_from_markdown(text: str) -> List[str]:
    """
    Extract image URLs from markdown text.

    Matches patterns like:
    - ![alt text](url)
    - ![](url)

    Args:
        text: Markdown text content

    Returns:
        List of image URLs found in the text
    """
    if not text:
        return []

    # Pattern matches ![optional alt text](url)
    pattern = r'!\[([^\]]*)\]\(([^)]+)\)'
    matches = re.findall(pattern, text)

    # Return only the URLs (second group)
    return [url.strip() for _, url in matches if url.strip()]


def remove_markdown_images(text: str) -> str:
    """
    Remove markdown image links from text, keeping only alt text.

    Converts ![alt text](url) to just "alt text"
    Converts ![](url) to empty string

    Args:
        text: Markdown text content

    Returns:
        Text with image links removed
    """
    if not text:
        return ""

    # Pattern to match markdown images and replace with alt text
    pattern = r'!\[([^\]]*)\]\([^)]+\)'
    return re.sub(pattern, r'\1', text)


def extract_and_clean_description(description_text: str) -> Tuple[str, List[str]]:
    """
    Extract image URLs from description and return cleaned text.

    This is useful for:
    1. Extracting reference images to pass to image generation
    2. Cleaning the description text to avoid rendering ![](url) as literal text

    Args:
        description_text: Page description that may contain markdown images

    Returns:
        Tuple of (cleaned_text, list_of_image_urls)
    """
    if not description_text:
        return "", []

    image_urls = extract_image_urls_from_markdown(description_text)
    cleaned_text = remove_markdown_images(description_text)

    return cleaned_text, image_urls


def is_mineru_image_path(path: str) -> bool:
    """
    Check if a path is a MinerU-extracted image path.

    MinerU paths typically look like:
    - /files/mineru/{extract_id}/{relative_path}
    - /api/files/mineru/...

    Args:
        path: File path to check

    Returns:
        True if path appears to be a MinerU image path
    """
    if not path:
        return False

    mineru_patterns = [
        '/files/mineru/',
        '/mineru/',
        'mineru/'
    ]

    return any(pattern in path.lower() for pattern in mineru_patterns)


def normalize_image_path(path: str, base_url: str = None) -> str:
    """
    Normalize an image path for use in requests.

    Handles:
    - Relative paths
    - MinerU paths
    - Full URLs

    Args:
        path: Original image path
        base_url: Optional base URL to prepend for relative paths

    Returns:
        Normalized path ready for fetching
    """
    if not path:
        return ""

    # Already a full URL
    if path.startswith(('http://', 'https://')):
        return path

    # Make sure path starts with /
    if not path.startswith('/'):
        path = '/' + path

    # Prepend base URL if provided
    if base_url:
        base_url = base_url.rstrip('/')
        return base_url + path

    return path


def _clean_json_response(text: str) -> str:
    """
    Clean AI response text to extract valid JSON.
    Handles markdown code blocks and extra text.
    """
    if not text:
        return "[]"

    # Remove markdown code blocks
    text = re.sub(r'```json\s*', '', text)
    text = re.sub(r'```\s*', '', text)
    text = text.strip()

    # Try to find JSON array or object
    # Look for array pattern
    array_match = re.search(r'\[[\s\S]*\]', text)
    if array_match:
        return array_match.group(0)

    # Look for object pattern
    obj_match = re.search(r'\{[\s\S]*\}', text)
    if obj_match:
        return obj_match.group(0)

    return text


def _parse_outline_json(text: str) -> List[Dict]:
    """
    Parse AI-generated outline JSON.

    Args:
        text: Raw AI response text

    Returns:
        List of outline items (either simple or part-based format)
    """
    cleaned = _clean_json_response(text)

    try:
        data = json.loads(cleaned)
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            # Single item, wrap in list
            return [data]
        else:
            logger.warning(f"Unexpected JSON type: {type(data)}")
            return []
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse outline JSON: {e}\nText: {cleaned[:500]}")
        return []


def flatten_outline_to_pages(outline_data: List[Dict]) -> List[Dict]:
    """
    Flatten outline data to a list of page dictionaries.

    Handles both formats:
    1. Simple format: [{"title": "...", "points": [...]}]
    2. Part-based format: [{"part": "...", "pages": [...]}]

    Args:
        outline_data: Parsed outline JSON

    Returns:
        List of page dicts with keys: title, points, part (optional)
    """
    pages = []

    for item in outline_data:
        if "pages" in item and "part" in item:
            # Part-based format
            part_name = item.get("part", "")
            for page in item.get("pages", []):
                pages.append({
                    "title": page.get("title", "Untitled"),
                    "points": page.get("points", []),
                    "part": part_name
                })
        else:
            # Simple format
            pages.append({
                "title": item.get("title", "Untitled"),
                "points": item.get("points", []),
                "part": None
            })

    return pages


def generate_outline_from_idea(
    idea_prompt: str,
    ai_provider,
    reference_files_content: Optional[List[Dict]] = None,
    language: str = 'zh'
) -> List[Dict]:
    """
    Generate PPT outline from an idea prompt.

    Args:
        idea_prompt: User's idea/topic for the presentation
        ai_provider: AI provider instance (e.g., GeminiProvider)
        reference_files_content: Optional reference files
        language: Output language code

    Returns:
        List of page dicts ready for database insertion
    """
    prompt = get_outline_generation_prompt(
        idea_prompt,
        reference_files_content=reference_files_content,
        language=language
    )

    result = ai_provider.generate_text(prompt)

    if not result.success:
        logger.error(f"AI outline generation failed: {result.error}")
        raise Exception(f"AI generation failed: {result.error}")

    outline_data = _parse_outline_json(result.text)
    return flatten_outline_to_pages(outline_data)


def parse_outline_text(
    outline_text: str,
    ai_provider,
    reference_files_content: Optional[List[Dict]] = None,
    language: str = 'zh'
) -> List[Dict]:
    """
    Parse user-provided outline text into structured format.

    Args:
        outline_text: User's outline text
        ai_provider: AI provider instance
        reference_files_content: Optional reference files
        language: Output language code

    Returns:
        List of page dicts ready for database insertion
    """
    prompt = get_outline_parsing_prompt(
        outline_text,
        reference_files_content=reference_files_content,
        language=language
    )

    result = ai_provider.generate_text(prompt)

    if not result.success:
        logger.error(f"AI outline parsing failed: {result.error}")
        raise Exception(f"AI parsing failed: {result.error}")

    outline_data = _parse_outline_json(result.text)
    return flatten_outline_to_pages(outline_data)


def generate_page_description(
    page_outline: Dict,
    page_index: int,
    full_outline: List[Dict],
    idea_prompt: str,
    ai_provider,
    reference_files_content: Optional[List[Dict]] = None,
    language: str = 'zh'
) -> Dict:
    """
    Generate description for a single page.

    Args:
        page_outline: The page's outline content (title, points)
        page_index: 1-based page index
        full_outline: Complete outline for context
        idea_prompt: Original user idea
        ai_provider: AI provider instance
        reference_files_content: Optional reference files
        language: Output language code

    Returns:
        Dict with description content
    """
    part_info = f"Current section: {page_outline.get('part', '')}" if page_outline.get('part') else ""

    prompt = get_page_description_prompt(
        outline=full_outline,
        page_outline=page_outline,
        page_index=page_index,
        idea_prompt=idea_prompt,
        part_info=part_info,
        reference_files_content=reference_files_content,
        language=language
    )

    result = ai_provider.generate_text(prompt)

    if not result.success:
        logger.error(f"AI description generation failed for page {page_index}: {result.error}")
        raise Exception(f"AI description generation failed: {result.error}")

    return {
        "text": result.text,
        "generated_at": None  # Will be set by caller
    }


def generate_page_image(
    page_description: str,
    page_index: int,
    outline_text: str,
    current_section: str,
    ai_provider,
    template_image: Optional[bytes] = None,
    material_images: Optional[List[bytes]] = None,
    extra_requirements: Optional[str] = None,
    language: str = 'zh'
) -> bytes:
    """
    Generate image for a single PPT page.

    Args:
        page_description: Page description text
        page_index: 1-based page index
        outline_text: Full outline text for context
        current_section: Current section/part name
        ai_provider: AI provider instance
        template_image: Optional template image bytes
        material_images: Optional material images
        extra_requirements: Extra requirements for generation
        language: Output language code

    Returns:
        Generated image bytes
    """
    has_template = template_image is not None
    has_materials = material_images is not None and len(material_images) > 0

    prompt = get_image_generation_prompt(
        page_desc=page_description,
        outline_text=outline_text,
        current_section=current_section,
        has_material_images=has_materials,
        extra_requirements=extra_requirements,
        language=language,
        has_template=has_template,
        page_index=page_index
    )

    # Prepare reference images
    reference_images = []
    if template_image:
        reference_images.append(template_image)
    if material_images:
        reference_images.extend(material_images[:2])  # Limit to avoid token overflow

    result = ai_provider.generate_image(
        prompt=prompt,
        reference_images=reference_images if reference_images else None
    )

    if not result.success:
        logger.error(f"AI image generation failed for page {page_index}: {result.error}")
        raise Exception(f"AI image generation failed: {result.error}")

    if not result.image_data:
        raise Exception("No image data in response")

    return result.image_data


def build_outline_text_from_pages(pages: List[Any]) -> str:
    """
    Build outline text from page objects for prompt context.

    Args:
        pages: List of PPTPage objects

    Returns:
        Formatted outline text string
    """
    lines = []
    current_part = None

    for i, page in enumerate(pages):
        outline = page.get_outline_content() if hasattr(page, 'get_outline_content') else page.get('outline_content')
        part = page.part if hasattr(page, 'part') else page.get('part')

        if part and part != current_part:
            current_part = part
            lines.append(f"\n## {part}")

        title = outline.get('title', 'Untitled') if outline else 'Untitled'
        lines.append(f"{i + 1}. {title}")

        points = outline.get('points', []) if outline else []
        for point in points:
            lines.append(f"   - {point}")

    return "\n".join(lines)
