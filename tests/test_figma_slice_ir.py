"""Unit tests for the slicer-agent IR path: designir_from_dict + gradients.

No network / no DB. Covers the dict -> DesignIR -> plugin-payload pipeline the
``code_figma_slice_generation`` workflow relies on, plus the defensive coercions
that keep a malformed model payload from raising.
"""
from backend.services.code.figma.ir import (
    IR_VERSION,
    Color,
    GradientStop,
    Paint,
    designir_from_dict,
    ir_to_plugin_payload,
)

# A realistic slicer-agent payload: a root frame with a solid background, a text
# node, a gradient block, and an image node (carrying a `crop` that must NOT
# leak into the IR). Boxes are absolute source-image pixels.
_SLICE_IR = {
    "ir_version": IR_VERSION,
    "source": "sliced",
    "name": "Landing thumbnail",
    "images": {"hero": "data:image/png;base64,AAAA"},
    "root": {
        "type": "FRAME",
        "name": "Page",
        "box": {"x": 0, "y": 0, "width": 1024, "height": 768},
        "fills": [{"type": "SOLID", "color": {"r": 1, "g": 1, "b": 1, "a": 1}}],
        "children": [
            {
                "type": "TEXT",
                "name": "Heading",
                "characters": "Build faster",
                "box": {"x": 80, "y": 96, "width": 400, "height": 48},
                "fills": [{"type": "SOLID", "color": "#1A1A1A"}],
                "text_style": {"font_family": "Inter", "font_size": 40, "font_weight": 700},
            },
            {
                "type": "RECTANGLE",
                "name": "CTA",
                "box": {"x": 80, "y": 160, "width": 200, "height": 56},
                "cornerRadius": 8,
                "fills": [
                    {
                        "type": "GRADIENT_LINEAR",
                        "angle": 90,
                        "stops": [
                            {"position": 0, "color": "#4F46E5"},
                            {"position": 1, "color": "#9333EA"},
                        ],
                    }
                ],
            },
            {
                "type": "IMAGE",
                "name": "Hero photo",
                "box": {"x": 560, "y": 96, "width": 380, "height": 280},
                "crop": {"x": 560, "y": 96, "w": 380, "h": 280},
                "fills": [{"type": "IMAGE", "imageRef": "hero"}],
            },
        ],
    },
}


def test_designir_from_dict_basic_shape():
    ir = designir_from_dict(_SLICE_IR)
    assert ir.ir_version == IR_VERSION
    assert ir.source == "sliced"
    assert ir.name == "Landing thumbnail"
    assert ir.images == {"hero": "data:image/png;base64,AAAA"}
    assert ir.root.type == "FRAME"
    assert ir.root.box is not None
    assert (ir.root.box.width, ir.root.box.height) == (1024, 768)
    assert len(ir.root.children) == 3


def test_crop_field_never_enters_ir():
    """`crop` is a container-only slicer input; it must be dropped from the IR."""
    ir = designir_from_dict(_SLICE_IR)
    image_node = ir.root.children[2]
    assert image_node.type == "IMAGE"
    # The IMAGE paint keeps its ref but the node carries no `crop` attribute.
    assert not hasattr(image_node, "crop")
    assert image_node.fills[0].type == "IMAGE"
    assert image_node.fills[0].image_ref == "hero"


def test_hex_and_text_color_parsing():
    ir = designir_from_dict(_SLICE_IR)
    text = ir.root.children[0]
    assert text.type == "TEXT"
    assert text.characters == "Build faster"
    assert text.fills, "text color (hex) should be parsed into a SOLID fill"
    color = text.fills[0].color
    assert color is not None
    # #1A1A1A -> 26/255 on each channel.
    assert abs(color.r - 26 / 255) < 1e-6
    assert text.text_style is not None
    assert text.text_style.font_size == 40
    assert text.text_style.font_weight == 700


def test_gradient_parsed_and_serialized_to_plugin():
    ir = designir_from_dict(_SLICE_IR)
    payload = ir_to_plugin_payload(ir)
    cta = payload["root"]["children"][1]
    assert cta["cornerRadius"] == 8
    gradient = cta["fills"][0]
    assert gradient["type"] == "GRADIENT_LINEAR"
    assert gradient["angle"] == 90
    assert len(gradient["gradientStops"]) == 2
    assert gradient["gradientStops"][0]["position"] == 0
    # #4F46E5 first channel
    assert abs(gradient["gradientStops"][0]["color"]["r"] - 0x4F / 255) < 1e-6


def test_plugin_payload_coordinates_are_parent_relative():
    ir = designir_from_dict(_SLICE_IR)
    payload = ir_to_plugin_payload(ir)
    # Root frame box origin is (0,0); image child at absolute x=560 stays 560.
    image = payload["root"]["children"][2]
    assert image["x"] == 560
    assert image["fills"][0] == {"type": "IMAGE", "imageRef": "hero", "opacity": 1.0}


def test_image_ref_to_data_url_wiring_round_trips():
    ir = designir_from_dict(_SLICE_IR)
    payload = ir_to_plugin_payload(ir)
    # Every IMAGE ref referenced in the tree must resolve in the images map.
    refs = _collect_image_refs(payload["root"])
    assert refs == {"hero"}
    assert all(ref in payload["images"] for ref in refs)


def test_255_color_normalization():
    ir = designir_from_dict(
        {"root": {"type": "RECTANGLE", "box": {"x": 0, "y": 0, "width": 10, "height": 10},
                  "fills": [{"type": "SOLID", "color": {"r": 255, "g": 0, "b": 128}}]}}
    )
    color = ir.root.fills[0].color
    assert abs(color.r - 1.0) < 1e-6
    assert abs(color.b - 128 / 255) < 1e-6


def test_malformed_payload_degrades_without_raising():
    # Garbage in: still returns a usable DesignIR with a default root.
    for junk in (None, [], "nope", 42, {"root": "broken"}, {"root": {"children": "x"}}):
        ir = designir_from_dict(junk)
        assert ir.root is not None
        # Must still serialize to a valid plugin payload.
        payload = ir_to_plugin_payload(ir)
        assert "root" in payload and "images" in payload


def test_unknown_node_type_falls_back():
    ir = designir_from_dict(
        {"root": {"type": "WEIRD", "box": {"x": 0, "y": 0, "width": 5, "height": 5}}}
    )
    # Unknown vector-ish type collapses to RECTANGLE; containers to FRAME.
    assert ir.root.type in ("RECTANGLE", "FRAME")


def test_camelcase_aliases_accepted():
    ir = designir_from_dict(
        {
            "root": {
                "type": "TEXT",
                "characters": "Hi",
                "box": {"x": 1, "y": 2, "w": 30, "h": 12},
                "textStyle": {"fontFamily": "Roboto", "fontSize": 14},
                "fills": [{"type": "SOLID", "color": {"r": 0, "g": 0, "b": 0}}],
            }
        }
    )
    assert ir.root.box.width == 30
    assert ir.root.text_style.font_family == "Roboto"
    assert ir.root.text_style.font_size == 14


def test_to_dict_preserves_gradient_paint():
    """A gradient built directly survives DesignIR.to_dict() (compactness aside)."""
    from backend.services.code.figma.ir import Box, DesignIR, IRNode

    ir = DesignIR(
        name="g",
        root=IRNode(
            type="RECTANGLE",
            box=Box(0, 0, 10, 10),
            fills=[
                Paint(
                    type="GRADIENT_LINEAR",
                    gradient_angle=45,
                    gradient_stops=[
                        GradientStop(0.0, Color(1, 0, 0, 1)),
                        GradientStop(1.0, Color(0, 0, 1, 1)),
                    ],
                )
            ],
        ),
    )
    payload = ir_to_plugin_payload(ir)
    fill = payload["root"]["fills"][0]
    assert fill["type"] == "GRADIENT_LINEAR"
    assert fill["angle"] == 45
    assert len(fill["gradientStops"]) == 2


def _collect_image_refs(node: dict) -> set:
    refs = set()
    for paint in node.get("fills", []):
        if paint.get("type") == "IMAGE" and paint.get("imageRef"):
            refs.add(paint["imageRef"])
    for child in node.get("children", []):
        refs |= _collect_image_refs(child)
    return refs
