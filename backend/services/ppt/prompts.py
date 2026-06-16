"""
PPT AI Service Prompts - centralized management of all AI prompt templates
"""
import json
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# Language configuration mapping
LANGUAGE_CONFIG = {
    'zh': {
        'name': '中文',
        'instruction': '请使用全中文输出。',
        'ppt_text': 'PPT文字请使用全中文。'
    },
    'ja': {
        'name': '日本語',
        'instruction': 'すべて日本語で出力してください。',
        'ppt_text': 'PPTのテキストは全て日本語で出力してください。'
    },
    'en': {
        'name': 'English',
        'instruction': 'Please output all in English.',
        'ppt_text': 'Use English for PPT text.'
    },
    'auto': {
        'name': 'Auto',
        'instruction': '',  # Auto mode does not add language restriction
        'ppt_text': ''
    }
}


def get_language_instruction(language: str = None) -> str:
    """
    Get language instruction text

    Args:
        language: Language code, defaults to 'zh' if None

    Returns:
        Language instruction, empty string for auto mode
    """
    lang = language if language else 'zh'
    config = LANGUAGE_CONFIG.get(lang, LANGUAGE_CONFIG['zh'])
    return config['instruction']


def get_ppt_language_instruction(language: str = None) -> str:
    """
    Get PPT text language instruction

    Args:
        language: Language code, defaults to 'zh' if None

    Returns:
        PPT language instruction, empty string for auto mode
    """
    lang = language if language else 'zh'
    config = LANGUAGE_CONFIG.get(lang, LANGUAGE_CONFIG['zh'])
    return config['ppt_text']


def _format_reference_files_xml(reference_files_content: Optional[List[Dict[str, str]]]) -> str:
    """
    Format reference files content as XML structure

    Args:
        reference_files_content: List of dicts with 'filename' and 'content' keys

    Returns:
        Formatted XML string
    """
    if not reference_files_content:
        return ""

    xml_parts = ["<uploaded_files>"]
    for file_info in reference_files_content:
        filename = file_info.get('filename', 'unknown')
        content = file_info.get('content', '')
        xml_parts.append(f'  <file name="{filename}">')
        xml_parts.append('    <content>')
        xml_parts.append(content)
        xml_parts.append('    </content>')
        xml_parts.append('  </file>')
    xml_parts.append('</uploaded_files>')
    xml_parts.append('')  # Empty line after XML

    return '\n'.join(xml_parts)


def get_outline_generation_prompt(idea_prompt: str, reference_files_content: Optional[List[Dict]] = None,
                                  language: str = None) -> str:
    """
    Generate PPT outline generation prompt

    Args:
        idea_prompt: User's PPT idea/topic
        reference_files_content: Optional reference files content
        language: Output language code

    Returns:
        Formatted prompt string
    """
    files_xml = _format_reference_files_xml(reference_files_content)

    prompt = f"""\
You are a helpful assistant that generates an outline for a ppt.

You can organize the content in two ways:

1. Simple format (for short PPTs without major sections):
[{{"title": "title1", "points": ["point1", "point2"]}}, {{"title": "title2", "points": ["point1", "point2"]}}]

2. Part-based format (for longer PPTs with major sections):
[
    {{
    "part": "Part 1: Introduction",
    "pages": [
        {{"title": "Welcome", "points": ["point1", "point2"]}},
        {{"title": "Overview", "points": ["point1", "point2"]}}
    ]
    }},
    {{
    "part": "Part 2: Main Content",
    "pages": [
        {{"title": "Topic 1", "points": ["point1", "point2"]}},
        {{"title": "Topic 2", "points": ["point1", "point2"]}}
    ]
    }}
]

Choose the format that best fits the content. Use parts when the PPT has clear major sections.
Unless otherwise specified, the first page should be kept simplest, containing only the title, subtitle, and presenter information.

The user's request: {idea_prompt}. Now generate the outline, don't include any other text.
{get_language_instruction(language)}
"""

    final_prompt = files_xml + prompt
    return final_prompt


def get_outline_parsing_prompt(outline_text: str, reference_files_content: Optional[List[Dict]] = None,
                                language: str = None) -> str:
    """
    Parse user-provided outline text prompt

    Args:
        outline_text: User's outline text
        reference_files_content: Optional reference files content
        language: Output language code

    Returns:
        Formatted prompt string
    """
    files_xml = _format_reference_files_xml(reference_files_content)

    prompt = f"""\
You are a helpful assistant that parses a user-provided PPT outline text into a structured format.

The user has provided the following outline text:

{outline_text}

Your task is to analyze this text and convert it into a structured JSON format WITHOUT modifying any of the original text content.
You should only reorganize and structure the existing content, preserving all titles, points, and text exactly as provided.

You can organize the content in two ways:

1. Simple format (for short PPTs without major sections):
[{{"title": "title1", "points": ["point1", "point2"]}}, {{"title": "title2", "points": ["point1", "point2"]}}]

2. Part-based format (for longer PPTs with major sections):
[
    {{
    "part": "Part 1: Introduction",
    "pages": [
        {{"title": "Welcome", "points": ["point1", "point2"]}},
        {{"title": "Overview", "points": ["point1", "point2"]}}
    ]
    }}
]

Important rules:
- DO NOT modify, rewrite, or change any text from the original outline
- DO NOT add new content that wasn't in the original text
- DO NOT remove any content from the original text
- Only reorganize the existing content into the structured format

Now parse the outline text above into the structured format. Return only the JSON, don't include any other text.
{get_language_instruction(language)}
"""

    final_prompt = files_xml + prompt
    return final_prompt


def get_page_description_prompt(outline: list, page_outline: dict, page_index: int,
                                 idea_prompt: str = None, part_info: str = "",
                                 reference_files_content: Optional[List[Dict]] = None,
                                 language: str = None) -> str:
    """
    Generate single page description prompt

    Args:
        outline: Complete outline
        page_outline: Current page's outline
        page_index: Page number (1-based)
        idea_prompt: Original user's idea
        part_info: Optional section info
        reference_files_content: Optional reference files content
        language: Output language code

    Returns:
        Formatted prompt string
    """
    files_xml = _format_reference_files_xml(reference_files_content)
    original_input = idea_prompt or ""

    prompt = f"""\
We are generating content descriptions for each PPT page.
User's original requirement: {original_input}

Complete outline:
{outline}
{part_info}

Now generate description for page {page_index}:
{page_outline}
{"**Unless specially required, the first page should be kept minimal with only title, subtitle, and presenter info.**" if page_index == 1 else ""}

【Important】The generated "page text" will be rendered directly on the PPT page. Please note:
1. Text should be concise, each bullet point within 15-25 characters
2. Well-organized, use list format
3. Avoid lengthy sentences
4. Ensure readability for presentation

Output format example:
Page title: [Title]
{"Subtitle: [Subtitle]" if page_index == 1 else ""}

Page text:
- [Point 1]
- [Point 2]
- [Point 3]

Other materials (if available, include markdown image links, formulas, tables, etc.)

{get_language_instruction(language)}
"""

    final_prompt = files_xml + prompt
    return final_prompt


def get_image_generation_prompt(page_desc: str, outline_text: str,
                                current_section: str,
                                has_material_images: bool = False,
                                extra_requirements: str = None,
                                language: str = None,
                                has_template: bool = True,
                                page_index: int = 1) -> str:
    """
    Generate image generation prompt

    Args:
        page_desc: Page description text
        outline_text: Outline text
        current_section: Current section
        has_material_images: Whether has material images
        extra_requirements: Extra requirements (may include style description)
        language: Output language
        has_template: Whether has template image
        page_index: Page index (1-based)

    Returns:
        Formatted prompt string
    """
    # Material images note
    material_images_note = ""
    if has_material_images:
        material_images_note = (
            "\n\nNote: " + ("Besides the template reference image (for style reference), additional material images are provided." if has_template else "Additional material images are provided.") +
            " These are elements you can select and use. You can choose appropriate images, icons, charts from these materials "
            "to integrate into the generated PPT page."
        )

    # Extra requirements
    extra_req_text = ""
    if extra_requirements and extra_requirements.strip():
        extra_req_text = f"\n\nExtra requirements (must follow):\n{extra_requirements}\n"

    # Template style guideline
    template_style_guideline = "- Color and design language strictly similar to template image." if has_template else "- Strictly follow the style description."
    forbidden_template_text_guideline = "- Only reference style design, do NOT include text from template.\n" if has_template else ""

    prompt = f"""\
You are an expert UI UX presentation designer focused on generating well-designed PPT pages.
Current PPT page description:
<page_description>
{page_desc}
</page_description>

<reference_information>
Complete PPT outline:
{outline_text}

Current section: {current_section}
</reference_information>


<design_guidelines>
- Text should be clear and sharp, 4K resolution, 16:9 aspect ratio.
{template_style_guideline}
- Design the best composition automatically, render all text from "page description" completely.
- Avoid markdown format symbols (like # and * etc.) unless necessary.
{forbidden_template_text_guideline}- Use appropriately sized decorative graphics or illustrations to fill empty spaces.
</design_guidelines>
{get_ppt_language_instruction(language)}
{material_images_note}{extra_req_text}

{"**Note: This is the cover page of PPT. Please use professional cover design techniques to highlight the title and ensure it catches the audience's attention immediately.**" if page_index == 1 else ""}
"""

    return prompt


def get_image_edit_prompt(edit_instruction: str, original_description: str = None) -> str:
    """
    Generate image edit prompt

    Args:
        edit_instruction: Edit instruction
        original_description: Original page description (optional)

    Returns:
        Formatted prompt string
    """
    if original_description:
        # Remove content after "other materials" to avoid being influenced
        if "其他页面素材" in original_description:
            original_description = original_description.split("其他页面素材")[0].strip()

        prompt = f"""\
The original page description for this PPT page is:
{original_description}

Now, modify this PPT page according to the following instruction: {edit_instruction}

Please maintain the original text content and design style, only make changes as instructed.
"""
    else:
        prompt = f"Modify this PPT page according to the following instruction: {edit_instruction}\nMaintain the original content structure and design style."

    return prompt


def get_outline_refinement_prompt(current_outline: List[Dict], user_requirement: str,
                                   idea_prompt: str = None,
                                   previous_requirements: Optional[List[str]] = None,
                                   reference_files_content: Optional[List[Dict]] = None,
                                   language: str = None) -> str:
    """
    Generate outline refinement prompt

    Args:
        current_outline: Current outline structure
        user_requirement: User's new requirement
        idea_prompt: Original idea prompt
        previous_requirements: Previous modification requirements
        reference_files_content: Reference files content
        language: Output language code

    Returns:
        Formatted prompt string
    """
    files_xml = _format_reference_files_xml(reference_files_content)

    # Handle empty outline
    if not current_outline or len(current_outline) == 0:
        outline_text = "(No current content)"
    else:
        outline_text = json.dumps(current_outline, ensure_ascii=False, indent=2)

    # Build previous requirements history
    previous_req_text = ""
    if previous_requirements and len(previous_requirements) > 0:
        prev_list = "\n".join([f"- {req}" for req in previous_requirements])
        previous_req_text = f"\n\nPrevious modification requirements:\n{prev_list}\n"

    # Build original input info
    original_input_text = ""
    if idea_prompt:
        original_input_text = f"\nOriginal PPT idea: {idea_prompt}\n"

    prompt = f"""\
You are a helpful assistant that modifies PPT outlines based on user requirements.
{original_input_text}
Current PPT outline structure:

{outline_text}
{previous_req_text}
**User's new requirement: {user_requirement}**

Please modify and adjust the outline based on user requirements. You can:
- Add, delete or rearrange pages
- Modify page titles and points
- Adjust organizational structure
- Add or delete parts
- Merge or split pages

Output format options:

1. Simple format (for short PPTs without major sections):
[{{"title": "title1", "points": ["point1", "point2"]}}]

2. Part-based format (for longer PPTs with major sections):
[
    {{
    "part": "Part 1: Introduction",
    "pages": [
        {{"title": "Welcome", "points": ["point1", "point2"]}}
    ]
    }}
]

Now modify the outline based on user requirements. Output only JSON format, no other text.
{get_language_instruction(language)}
"""

    final_prompt = files_xml + prompt
    return final_prompt


def get_descriptions_refinement_prompt(current_descriptions: List[Dict], user_requirement: str,
                                       idea_prompt: str = None,
                                       outline: List[Dict] = None,
                                       previous_requirements: Optional[List[str]] = None,
                                       reference_files_content: Optional[List[Dict]] = None,
                                       language: str = None) -> str:
    """
    Generate descriptions refinement prompt

    Args:
        current_descriptions: Current page descriptions list
        user_requirement: User's new requirement
        idea_prompt: Original idea prompt
        outline: Complete outline structure
        previous_requirements: Previous modification requirements
        reference_files_content: Reference files content
        language: Output language code

    Returns:
        Formatted prompt string
    """
    files_xml = _format_reference_files_xml(reference_files_content)

    # Build previous requirements history
    previous_req_text = ""
    if previous_requirements and len(previous_requirements) > 0:
        prev_list = "\n".join([f"- {req}" for req in previous_requirements])
        previous_req_text = f"\n\nPrevious modification requirements:\n{prev_list}\n"

    # Build original input info
    original_input_text = ""
    if idea_prompt:
        original_input_text = f"\nOriginal PPT idea: {idea_prompt}\n"

    # Build outline text
    outline_text = ""
    if outline:
        outline_json = json.dumps(outline, ensure_ascii=False, indent=2)
        outline_text = f"\n\nComplete PPT outline:\n{outline_json}\n"

    # Build all descriptions summary
    all_descriptions_text = "Current page descriptions:\n\n"
    has_any_description = False
    for desc in current_descriptions:
        page_num = desc.get('index', 0) + 1
        title = desc.get('title', 'Untitled')
        content = desc.get('description_content', '')
        if isinstance(content, dict):
            content = content.get('text', '')

        if content:
            has_any_description = True
            all_descriptions_text += f"--- Page {page_num}: {title} ---\n{content}\n\n"
        else:
            all_descriptions_text += f"--- Page {page_num}: {title} ---\n(No current content)\n\n"

    if not has_any_description:
        all_descriptions_text = "Current page descriptions:\n\n(No content, need to generate based on outline)\n\n"

    prompt = f"""\
You are a helpful assistant that modifies PPT page descriptions based on user requirements.
{original_input_text}{outline_text}
{all_descriptions_text}
{previous_req_text}
**User's new requirement: {user_requirement}**

Please modify all page descriptions based on user requirements. You can:
- Modify page titles and content
- Adjust detail level
- Add or remove points
- Adjust structure and expression

Generate modified description for each page in this format:

Page title: [Title]

Page text:
- [Point 1]
- [Point 2]
...

Return a JSON array, each element is a string corresponding to each page's modified description (in order).

Example output format:
[
    "Page title: AI History\\nPage text:\\n- 1950: Turing Test...",
    "Page title: AI Development\\nPage text:\\n- 1950s: Symbolism...",
    ...
]

Now modify all page descriptions. Output only JSON array, no other text.
{get_language_instruction(language)}
"""

    final_prompt = files_xml + prompt
    return final_prompt


def get_clean_background_prompt() -> str:
    """
    Generate clean background prompt (remove text and illustrations)
    Used to extract pure background from complete PPT page
    """
    prompt = """\
You are a professional image text & object removal expert. Your task is to remove all text and illustrations from the original image, outputting a clean, pure background plate without any text or chart content.
<requirements>
- Completely remove all text, illustrations, charts from the page
- Maintain the integrity of original background design (including gradients, textures, patterns, lines, color blocks)
- For background areas covered by foreground elements, intelligently fill in to keep background seamless
- Output image dimensions, style, colors must match original exactly
- Do NOT add any elements
</requirements>

Note: **ALL** text and charts should be completely removed, output should NOT contain any text or charts.
"""
    return prompt


def get_text_attribute_extraction_prompt(ocr_text: Optional[str] = None) -> str:
    """
    Generate text attribute extraction prompt for a single cropped text region.
    Used to extract precise color information from cropped text images.

    Args:
        ocr_text: Optional OCR-detected text content for reference

    Returns:
        Formatted prompt string
    """
    reference_text = f'\nReference text from OCR: "{ocr_text}"' if ocr_text else ""

    prompt = f"""\
Your task is to accurately identify the text content and style from this cropped text region image, returning results in JSON format.

## Core Task
Please carefully observe the image and precisely identify:
1. **Text Content** - Output the actual text characters you see
2. **Color** - The actual color of each character/word (in hex format)
3. **Spaces** - Precisely identify the position and number of spaces in the text
4. **Formulas** - If it's a mathematical formula, output in LaTeX format
{reference_text}

## Output Format
{{
    "colored_segments": [
        {{"text": "Example Text", "color": "#000000"}},
        {{"text": "Highlighted", "color": "#26397A"}},
        {{"text": "x^2 + y^2 = z^2", "color": "#FF0000", "is_latex": true}}
    ]
}}

## Important Notes
- Each segment should be a continuous piece of text with the same color
- Use exact hex color codes (e.g., #FF5733, not "orange")
- Preserve exact spacing and punctuation
- For LaTeX formulas, set "is_latex": true

Now analyze the image and output the JSON result only, no other text.
"""
    return prompt


def get_batch_text_attribute_extraction_prompt(text_elements: List[Dict[str, str]]) -> str:
    """
    Generate batch text attribute extraction prompt for analyzing all text elements.
    Used to extract layout attributes (bold, italic, underline, alignment) from full page.

    Args:
        text_elements: List of dicts with 'id' and 'text' keys representing text regions

    Returns:
        Formatted prompt string
    """
    # Build text elements reference
    elements_list = "\n".join([
        f'- Element {elem.get("id", i)}: "{elem.get("text", "")}"'
        for i, elem in enumerate(text_elements)
    ])

    prompt = f"""\
Your task is to analyze the style attributes of all text elements in this PPT page image.

## Text Elements to Analyze
{elements_list}

## Attributes to Extract (for each element)
1. **font_color** - Text color (hex format, e.g., #000000)
2. **is_bold** - Whether the text is bold (true/false)
3. **is_italic** - Whether the text is italic (true/false)
4. **is_underline** - Whether the text has underline (true/false)
5. **text_alignment** - Text alignment: "left", "center", "right", or "justify"

## Output Format
Return a JSON object mapping element IDs to their attributes:

{{
    "0": {{
        "font_color": "#000000",
        "is_bold": true,
        "is_italic": false,
        "is_underline": false,
        "text_alignment": "center"
    }},
    "1": {{
        "font_color": "#333333",
        "is_bold": false,
        "is_italic": false,
        "is_underline": false,
        "text_alignment": "left"
    }}
}}

## Important Notes
- Title text is usually bold and centered
- Bullet points are usually left-aligned
- Use exact hex color codes
- When uncertain, default to: not bold, not italic, no underline, left alignment

Now analyze the image and output the JSON result only, no other text.
"""
    return prompt


def get_quality_enhancement_prompt(repaired_regions: Optional[List[Dict]] = None) -> str:
    """
    Generate quality enhancement prompt for fixing inpainting artifacts.
    Used to repair visible traces left by background removal process.

    Args:
        repaired_regions: Optional list of dicts with bbox coordinates of repaired regions
                         Each dict should have 'x', 'y', 'width', 'height' keys

    Returns:
        Formatted prompt string
    """
    # Build region coordinates if provided
    region_info = ""
    if repaired_regions:
        region_lines = []
        for i, region in enumerate(repaired_regions):
            x = region.get('x', 0)
            y = region.get('y', 0)
            w = region.get('width', 0)
            h = region.get('height', 0)
            region_lines.append(f"- Region {i + 1}: x={x}, y={y}, width={w}, height={h}")
        region_info = "\n\n<repaired_regions>\nThe following regions were processed by the removal tool:\n" + "\n".join(region_lines) + "\n</repaired_regions>"

    prompt = f"""\
You are a professional image repair expert. This PPT page image has just undergone text/object removal, and the removal tool left some repair traces in the specified regions, including:
- Uneven color blocks, inconsistent colors
- Blurry patches or smearing traces
- Areas that don't blend with surrounding background
- Possible texture breaks or pattern discontinuities
{region_info}

<requirements>
- Focus on repairing the marked regions above
- Maintain texture, color, and pattern continuity
- Do NOT add any text, charts, illustrations, or other elements
- Keep other areas pixel-identical to the original image
- Output image dimensions must match the original exactly
</requirements>

<important>
The goal is to make the repaired areas blend seamlessly with the surrounding background, as if there was never any content there. The output should be a clean background without any visible repair traces.
</important>
"""
    return prompt


def get_image_edit_with_region_prompt(
    edit_instruction: str,
    original_description: str = None,
    has_selection_mask: bool = False,
    has_new_material: bool = False
) -> str:
    """
    Generate image edit prompt with support for region selection and new materials.

    Args:
        edit_instruction: User's edit instruction
        original_description: Original page description (optional)
        has_selection_mask: Whether user has provided a selection mask/region
        has_new_material: Whether new material images are provided

    Returns:
        Formatted prompt string
    """
    desc_section = ""
    if original_description:
        # Remove content after "other materials" to avoid being influenced
        if "其他页面素材" in original_description:
            original_description = original_description.split("其他页面素材")[0].strip()
        if "Other materials" in original_description:
            original_description = original_description.split("Other materials")[0].strip()
        desc_section = f"""
The original page description for this PPT page is:
<original_description>
{original_description}
</original_description>
"""

    region_instruction = ""
    if has_selection_mask:
        region_instruction = """
<selection_context>
The user has provided a selection mask highlighting specific regions. The reference images include:
- The original PPT page
- A mask image showing user-selected regions (highlighted areas)

Please intelligently determine the user's intent:
- If the mask highlights areas the user wants to REPLACE, generate new content for those regions
- If the mask highlights areas the user wants to KEEP, preserve those areas while modifying the rest
- Use context and the edit instruction to make the right judgment
</selection_context>
"""

    material_instruction = ""
    if has_new_material:
        material_instruction = """
<new_materials>
New material images are provided. Please intelligently incorporate them into the edited result:
- Choose the most appropriate placement for the new materials
- Blend them naturally with the existing design
- Maintain consistent style with the original page
</new_materials>
"""

    prompt = f"""\
You are an expert PPT page editor. Your task is to modify the provided PPT page image according to the user's instruction.
{desc_section}
<edit_instruction>
{edit_instruction}
</edit_instruction>
{region_instruction}{material_instruction}
<requirements>
- Maintain the original text content and design style unless explicitly instructed to change
- Preserve 4K resolution and 16:9 aspect ratio
- Keep text clear and sharp
- Make modifications precise and targeted
- Ensure visual consistency with the original design
</requirements>

Now apply the edit instruction to the image.
"""
    return prompt
