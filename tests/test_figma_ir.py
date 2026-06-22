"""Unit tests for the Figma Design IR converters (no network / no DB)."""
from backend.services.code.figma.ir import (
    IR_VERSION,
    DesignIR,
    figma_node_to_ir,
    image_design_ir,
    ir_to_plugin_payload,
)

_FIGMA_FRAME = {
    "id": "1:2",
    "name": "Card",
    "type": "FRAME",
    "absoluteBoundingBox": {"x": 100, "y": 200, "width": 300, "height": 150},
    "cornerRadius": 12,
    "fills": [{"type": "SOLID", "color": {"r": 1, "g": 1, "b": 1, "a": 1}}],
    "children": [
        {
            "id": "1:3",
            "name": "Title",
            "type": "TEXT",
            "characters": "Hello",
            "absoluteBoundingBox": {"x": 120, "y": 220, "width": 200, "height": 24},
            "style": {
                "fontFamily": "Inter",
                "fontSize": 18,
                "fontWeight": 600,
                "textAlignHorizontal": "LEFT",
            },
            "fills": [{"type": "SOLID", "color": {"r": 0.1, "g": 0.1, "b": 0.1, "a": 1}}],
        },
        {
            "id": "1:4",
            "name": "Hidden",
            "type": "RECTANGLE",
            "visible": False,
            "absoluteBoundingBox": {"x": 0, "y": 0, "width": 10, "height": 10},
        },
    ],
}


def test_figma_node_to_ir_structure_and_tokens():
    design = figma_node_to_ir(_FIGMA_FRAME, file_name="My File")
    assert design.ir_version == IR_VERSION
    assert design.source == "figma"
    assert design.name == "My File"

    root = design.root
    assert root.type == "FRAME"
    assert root.corner_radius == 12
    # The invisible child is dropped.
    assert len(root.children) == 1
    text = root.children[0]
    assert text.type == "TEXT"
    assert text.characters == "Hello"
    assert text.text_style.font_family == "Inter"
    assert text.text_style.font_size == 18
    assert text.text_style.font_weight == 600

    # Tokens aggregate de-duplicated colors + fonts.
    assert len(design.tokens.colors) == 2  # white frame + dark text
    assert len(design.tokens.fonts) == 1


def test_ir_to_plugin_payload_relative_coords():
    design = figma_node_to_ir(_FIGMA_FRAME, file_name="F")
    payload = ir_to_plugin_payload(design)
    assert payload["root"]["x"] == 0  # root is its own origin
    child = payload["root"]["children"][0]
    # Child absolute (120,220) relative to frame origin (100,200) -> (20,20).
    assert child["x"] == 20
    assert child["y"] == 20
    assert child["characters"] == "Hello"
    assert child["fontSize"] == 18
    # Solid fill collapses to r/g/b with opacity folded in.
    assert payload["root"]["fills"][0]["type"] == "SOLID"


def test_image_design_ir_wraps_single_image():
    data_url = "data:image/png;base64,AAAA"
    design = image_design_ir(name="Preview", image_data_url=data_url, width=800, height=600)
    assert isinstance(design, DesignIR)
    assert design.source == "preview_image"
    assert design.images["preview"] == data_url
    payload = ir_to_plugin_payload(design)
    image_node = payload["root"]["children"][0]
    assert image_node["type"] == "IMAGE"
    assert image_node["fills"][0]["imageRef"] == "preview"


def test_partial_payload_does_not_crash():
    # Defensive: a node missing most fields still converts.
    design = figma_node_to_ir({"type": "FRAME"}, file_name="empty")
    payload = ir_to_plugin_payload(design)
    assert payload["root"]["type"] == "FRAME"
