"""
Design IR — the canonical intermediate representation between the platform and
Figma, in both directions.

The IR is a deliberately small subset of Figma's node schema (FRAME / GROUP /
RECTANGLE / TEXT / IMAGE) plus a top-level ``tokens`` summary (palette / fonts /
spacings). It is the single bridge type:

- Import:  Figma REST node tree  --figma_node_to_ir-->  DesignIR
- Export:  DesignIR  --ir_to_plugin_payload-->  Figma-plugin-consumable JSON

Colors are kept as 0..1 RGBA throughout (matching the Figma REST + Plugin APIs),
so neither direction has to rescale by 255.

This module is pure data + pure functions — no Flask / DB / network — so it is
trivially unit-testable and safe to import from a background workflow thread.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

IR_VERSION = "1.0"

# Node types we model. Anything else from Figma is mapped to the closest of these
# (containers -> FRAME, vectors/shapes -> RECTANGLE) so the IR stays bounded.
NODE_TYPES = ("DOCUMENT", "FRAME", "GROUP", "RECTANGLE", "TEXT", "IMAGE")

_CONTAINER_FIGMA_TYPES = {
    "DOCUMENT",
    "CANVAS",
    "FRAME",
    "GROUP",
    "SECTION",
    "COMPONENT",
    "COMPONENT_SET",
    "INSTANCE",
}


@dataclass
class Color:
    """RGBA in 0..1, matching Figma."""

    r: float = 0.0
    g: float = 0.0
    b: float = 0.0
    a: float = 1.0


@dataclass
class GradientStop:
    """A single linear-gradient stop: position 0..1 + color (rgba 0..1)."""

    position: float = 0.0
    color: Color = field(default_factory=Color)


@dataclass
class Paint:
    """A single fill or stroke entry."""

    type: str = "SOLID"  # SOLID | IMAGE | GRADIENT_LINEAR | ...
    color: Optional[Color] = None
    opacity: float = 1.0
    image_ref: Optional[str] = None  # IMAGE fill -> key into DesignIR.images
    # GRADIENT_LINEAR only: ordered stops + angle in degrees (0 = left->right,
    # 90 = top->bottom). The plugin turns (stops, angle) into a gradientTransform.
    gradient_stops: List[GradientStop] = field(default_factory=list)
    gradient_angle: Optional[float] = None


@dataclass
class Box:
    """absoluteBoundingBox — the key to reconstructing layout."""

    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0


@dataclass
class TextStyle:
    font_family: Optional[str] = None
    font_size: Optional[float] = None
    font_weight: Optional[int] = None
    line_height_px: Optional[float] = None
    letter_spacing: Optional[float] = None
    text_align_horizontal: Optional[str] = None  # LEFT | CENTER | RIGHT | JUSTIFIED
    fills: List[Paint] = field(default_factory=list)


@dataclass
class IRNode:
    id: str = ""
    name: str = ""
    type: str = "FRAME"
    box: Optional[Box] = None
    fills: List[Paint] = field(default_factory=list)
    strokes: List[Paint] = field(default_factory=list)
    stroke_weight: Optional[float] = None
    corner_radius: Optional[float] = None
    opacity: float = 1.0
    characters: Optional[str] = None  # TEXT only
    text_style: Optional[TextStyle] = None
    layout_mode: Optional[str] = None  # NONE | HORIZONTAL | VERTICAL (auto-layout)
    item_spacing: Optional[float] = None
    padding: Optional[Dict[str, float]] = None  # {top,right,bottom,left}
    children: List["IRNode"] = field(default_factory=list)


@dataclass
class DesignTokens:
    colors: List[Color] = field(default_factory=list)
    fonts: List[TextStyle] = field(default_factory=list)
    spacings: List[float] = field(default_factory=list)


@dataclass
class DesignIR:
    ir_version: str = IR_VERSION
    source: str = "figma"  # figma | html | preview_image | sliced
    name: str = ""
    root: IRNode = field(default_factory=IRNode)
    tokens: DesignTokens = field(default_factory=DesignTokens)
    images: Dict[str, str] = field(default_factory=dict)  # image_ref -> data URL

    def to_dict(self) -> Dict[str, Any]:
        return _clean(asdict(self))

    def to_prompt_text(self, max_chars: int = 24_000) -> str:
        """A compact JSON rendering for injecting into a model prompt."""
        import json

        text = json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))
        return text[:max_chars]


# --------------------------------------------------------------------------- #
# Import:  Figma REST node  ->  DesignIR
# --------------------------------------------------------------------------- #
def figma_node_to_ir(figma_node: dict, *, file_name: str = "", source: str = "figma") -> DesignIR:
    """Convert a single Figma node object (a frame/document) into a DesignIR.

    ``figma_node`` is the raw node dict from the Figma REST API — i.e. the
    ``document`` object of a node (``/v1/files/:key/nodes`` -> nodes[id].document)
    or the file's top-level ``document``. Conversion is defensive: every field is
    read with ``.get`` so partial / unexpected payloads degrade rather than throw.
    """
    tokens = DesignTokens()
    root = _convert_node(figma_node or {}, tokens)
    return DesignIR(
        ir_version=IR_VERSION,
        source=source,
        name=file_name or (figma_node or {}).get("name") or "Figma design",
        root=root,
        tokens=tokens,
    )


def _convert_node(node: dict, tokens: DesignTokens) -> IRNode:
    figma_type = (node.get("type") or "FRAME").upper()
    ir = IRNode(
        id=str(node.get("id") or ""),
        name=str(node.get("name") or ""),
        type=_map_type(figma_type),
        box=_convert_box(node.get("absoluteBoundingBox")),
        fills=_convert_paints(node.get("fills"), tokens),
        strokes=_convert_paints(node.get("strokes"), tokens),
        stroke_weight=_as_float(node.get("strokeWeight")),
        corner_radius=_convert_corner_radius(node),
        opacity=_as_float(node.get("opacity"), default=1.0) or 1.0,
        layout_mode=node.get("layoutMode"),
        item_spacing=_as_float(node.get("itemSpacing")),
        padding=_convert_padding(node),
    )
    _collect_spacing(tokens, ir.item_spacing)

    if figma_type == "TEXT":
        ir.characters = node.get("characters")
        ir.text_style = _convert_text_style(node.get("style"), ir.fills, tokens)

    for child in node.get("children") or []:
        if isinstance(child, dict) and child.get("visible", True) is not False:
            ir.children.append(_convert_node(child, tokens))
    return ir


def _map_type(figma_type: str) -> str:
    if figma_type in _CONTAINER_FIGMA_TYPES:
        return "FRAME" if figma_type != "GROUP" else "GROUP"
    if figma_type == "TEXT":
        return "TEXT"
    # VECTOR / RECTANGLE / ELLIPSE / LINE / STAR / BOOLEAN_OPERATION -> RECTANGLE
    return "RECTANGLE"


def _convert_box(box: Any) -> Optional[Box]:
    if not isinstance(box, dict):
        return None
    return Box(
        x=_as_float(box.get("x"), 0.0) or 0.0,
        y=_as_float(box.get("y"), 0.0) or 0.0,
        width=_as_float(box.get("width"), 0.0) or 0.0,
        height=_as_float(box.get("height"), 0.0) or 0.0,
    )


def _convert_paints(paints: Any, tokens: DesignTokens) -> List[Paint]:
    result: List[Paint] = []
    if not isinstance(paints, list):
        return result
    for paint in paints:
        if not isinstance(paint, dict) or paint.get("visible") is False:
            continue
        ptype = (paint.get("type") or "SOLID").upper()
        color = None
        raw_color = paint.get("color")
        if isinstance(raw_color, dict):
            color = Color(
                r=_as_float(raw_color.get("r"), 0.0) or 0.0,
                g=_as_float(raw_color.get("g"), 0.0) or 0.0,
                b=_as_float(raw_color.get("b"), 0.0) or 0.0,
                a=_as_float(raw_color.get("a"), 1.0) if raw_color.get("a") is not None else 1.0,
            )
            _collect_color(tokens, color)
        result.append(
            Paint(
                type=ptype,
                color=color,
                opacity=_as_float(paint.get("opacity"), 1.0) or 1.0,
                image_ref=paint.get("imageRef"),
            )
        )
    return result


def _convert_corner_radius(node: dict) -> Optional[float]:
    if node.get("cornerRadius") is not None:
        return _as_float(node.get("cornerRadius"))
    radii = node.get("rectangleCornerRadii")
    if isinstance(radii, list) and radii:
        # Collapse to the max corner; the IR keeps a single scalar by design.
        nums = [n for n in (_as_float(r) for r in radii) if n is not None]
        return max(nums) if nums else None
    return None


def _convert_padding(node: dict) -> Optional[Dict[str, float]]:
    keys = ("paddingTop", "paddingRight", "paddingBottom", "paddingLeft")
    if not any(node.get(k) is not None for k in keys):
        return None
    return {
        "top": _as_float(node.get("paddingTop"), 0.0) or 0.0,
        "right": _as_float(node.get("paddingRight"), 0.0) or 0.0,
        "bottom": _as_float(node.get("paddingBottom"), 0.0) or 0.0,
        "left": _as_float(node.get("paddingLeft"), 0.0) or 0.0,
    }


def _convert_text_style(style: Any, fills: List[Paint], tokens: DesignTokens) -> TextStyle:
    style = style if isinstance(style, dict) else {}
    text_style = TextStyle(
        font_family=style.get("fontFamily"),
        font_size=_as_float(style.get("fontSize")),
        font_weight=_as_int(style.get("fontWeight")),
        line_height_px=_as_float(style.get("lineHeightPx")),
        letter_spacing=_as_float(style.get("letterSpacing")),
        text_align_horizontal=style.get("textAlignHorizontal"),
        fills=list(fills),
    )
    if text_style.font_family or text_style.font_size:
        _collect_font(tokens, text_style)
    return text_style


# --------------------------------------------------------------------------- #
# Export:  DesignIR  ->  Figma-plugin-consumable payload
# --------------------------------------------------------------------------- #
def ir_to_plugin_payload(ir: DesignIR) -> Dict[str, Any]:
    """Flatten a DesignIR into the structure the Figma plugin consumes.

    The main transform is absolute -> parent-relative coordinates (Figma's
    ``node.x/y`` are relative to the parent), plus dropping fields the plugin
    does not use. Colors stay 0..1 RGBA.
    """
    origin = ir.root.box or Box()
    return {
        "ir_version": ir.ir_version,
        "source": ir.source,
        "name": ir.name,
        "images": ir.images,
        "root": _node_to_plugin(ir.root, parent_x=origin.x, parent_y=origin.y),
    }


def _node_to_plugin(node: IRNode, *, parent_x: float, parent_y: float) -> Dict[str, Any]:
    box = node.box or Box()
    payload: Dict[str, Any] = {
        "type": node.type,
        "name": node.name or node.type.title(),
        "x": round(box.x - parent_x, 2),
        "y": round(box.y - parent_y, 2),
        "width": round(box.width, 2),
        "height": round(box.height, 2),
        "opacity": node.opacity,
        "fills": [_paint_to_plugin(p) for p in node.fills if _paint_to_plugin(p)],
    }
    if node.strokes:
        payload["strokes"] = [_paint_to_plugin(p) for p in node.strokes if _paint_to_plugin(p)]
        if node.stroke_weight is not None:
            payload["strokeWeight"] = node.stroke_weight
    if node.corner_radius is not None:
        payload["cornerRadius"] = node.corner_radius
    if node.type == "TEXT":
        payload["characters"] = node.characters or ""
        if node.text_style:
            payload["fontSize"] = node.text_style.font_size or 16
            payload["fontFamily"] = node.text_style.font_family or "Inter"
            payload["fontWeight"] = node.text_style.font_weight
            payload["textAlignHorizontal"] = node.text_style.text_align_horizontal or "LEFT"
            # The plugin renders these (build.ts/code.ts) but they were being
            # dropped here — forward them when present.
            if node.text_style.line_height_px is not None:
                payload["lineHeight"] = node.text_style.line_height_px
            if node.text_style.letter_spacing is not None:
                payload["letterSpacing"] = node.text_style.letter_spacing
    # Auto-layout hints for FRAME containers. The plugin applies these only when
    # it's visually safe (single-axis, non-overlapping, ~uniform gaps); otherwise
    # children keep absolute x/y — see buildFrame / maybeApplyAutoLayout.
    if node.layout_mode in ("HORIZONTAL", "VERTICAL"):
        payload["layoutMode"] = node.layout_mode
        if node.item_spacing is not None:
            payload["itemSpacing"] = node.item_spacing
        if node.padding:
            payload["padding"] = node.padding
    if node.children:
        payload["children"] = [
            _node_to_plugin(child, parent_x=box.x, parent_y=box.y) for child in node.children
        ]
    return payload


def _paint_to_plugin(paint: Paint) -> Optional[Dict[str, Any]]:
    if paint.type == "IMAGE" and paint.image_ref:
        return {"type": "IMAGE", "imageRef": paint.image_ref, "opacity": paint.opacity}
    if paint.type == "GRADIENT_LINEAR" and paint.gradient_stops:
        stops = [
            {
                "position": round(_clamp01(stop.position), 4),
                "color": {
                    "r": stop.color.r,
                    "g": stop.color.g,
                    "b": stop.color.b,
                    "a": stop.color.a,
                },
            }
            for stop in paint.gradient_stops
        ]
        return {
            "type": "GRADIENT_LINEAR",
            "gradientStops": stops,
            "angle": paint.gradient_angle if paint.gradient_angle is not None else 90.0,
            "opacity": round(paint.opacity, 4),
        }
    if paint.color is None:
        return None
    return {
        "type": "SOLID",
        "color": {"r": paint.color.r, "g": paint.color.g, "b": paint.color.b},
        "opacity": round(paint.opacity * paint.color.a, 4),
    }


# --------------------------------------------------------------------------- #
# Preview-image export: wrap a single rendered image as a 1-node IR
# --------------------------------------------------------------------------- #
def image_design_ir(
    *, name: str, image_data_url: str, width: float, height: float
) -> DesignIR:
    """Build a DesignIR whose root frame is filled by a single image.

    Used by the preview-image export path: the rendered PNG goes straight onto a
    Figma frame as an image fill (100% fidelity, zero model calls).
    """
    ref = "preview"
    frame = IRNode(
        id="frame",
        name=name or "Preview",
        type="FRAME",
        box=Box(0, 0, width, height),
        children=[
            IRNode(
                id="image",
                name="Preview image",
                type="IMAGE",
                box=Box(0, 0, width, height),
                fills=[Paint(type="IMAGE", image_ref=ref)],
            )
        ],
    )
    return DesignIR(
        ir_version=IR_VERSION,
        source="preview_image",
        name=name or "Preview",
        root=frame,
        images={ref: image_data_url},
    )


# --------------------------------------------------------------------------- #
# Deserialize:  plain dict (slicer agent output)  ->  DesignIR
# --------------------------------------------------------------------------- #
def designir_from_dict(data: Any) -> DesignIR:
    """Build a DesignIR from an untrusted plain dict (the slicer agent's output).

    The inverse of ``DesignIR.to_dict()`` but defensive in the same spirit as
    ``figma_node_to_ir``: every field is read with ``.get`` / coerced, so a
    partial or malformed payload degrades to sane defaults instead of raising.
    Tolerant of common variants the model may emit (camelCase keys, hex / 0..255
    colors, ``w``/``h`` boxes). Unknown keys are ignored — in particular an IMAGE
    node's ``crop`` (consumed only by the in-container slicer) deliberately never
    enters the IR. ``tokens`` are not reconstructed (unused downstream).
    """
    data = data if isinstance(data, dict) else {}
    images = data.get("images")
    images = (
        {str(k): str(v) for k, v in images.items() if v} if isinstance(images, dict) else {}
    )
    return DesignIR(
        ir_version=str(data.get("ir_version") or data.get("irVersion") or IR_VERSION),
        source=str(data.get("source") or "sliced"),
        name=str(data.get("name") or "Design"),
        root=_node_from_dict(data.get("root")),
        tokens=DesignTokens(),
        images=images,
    )


def _pick(data: dict, *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return None


def _node_from_dict(data: Any) -> IRNode:
    data = data if isinstance(data, dict) else {}
    raw_type = str(_pick(data, "type") or "FRAME").upper()
    node_type = raw_type if raw_type in NODE_TYPES else _map_type(raw_type)
    fills = _paints_from_dict(_pick(data, "fills"))
    node = IRNode(
        id=str(_pick(data, "id") or ""),
        name=str(_pick(data, "name") or ""),
        type=node_type,
        box=_box_from_dict(_pick(data, "box")),
        fills=fills,
        strokes=_paints_from_dict(_pick(data, "strokes")),
        stroke_weight=_as_float(_pick(data, "stroke_weight", "strokeWeight")),
        corner_radius=_as_float(_pick(data, "corner_radius", "cornerRadius")),
        opacity=_as_float(_pick(data, "opacity"), 1.0) or 1.0,
        layout_mode=_str_or_none(_pick(data, "layout_mode", "layoutMode")),
        item_spacing=_as_float(_pick(data, "item_spacing", "itemSpacing")),
        padding=_padding_from_dict(_pick(data, "padding")),
    )
    if node_type == "TEXT":
        chars = _pick(data, "characters", "text")
        node.characters = str(chars) if chars is not None else ""
        style_dict = _pick(data, "text_style", "textStyle")
        node.text_style = _textstyle_from_dict(style_dict, fills)
        if not node.fills:
            # Some models park the text color only under the style object.
            ts_color = _color_from_dict(
                _pick(style_dict, "color", "fill") if isinstance(style_dict, dict) else None
            )
            if ts_color:
                node.fills = [Paint(type="SOLID", color=ts_color)]
    children = _pick(data, "children")
    if isinstance(children, list):
        node.children = [_node_from_dict(c) for c in children if isinstance(c, dict)]
    return node


def _box_from_dict(data: Any) -> Optional[Box]:
    if not isinstance(data, dict):
        return None
    return Box(
        x=_as_float(_pick(data, "x"), 0.0) or 0.0,
        y=_as_float(_pick(data, "y"), 0.0) or 0.0,
        width=_as_float(_pick(data, "width", "w"), 0.0) or 0.0,
        height=_as_float(_pick(data, "height", "h"), 0.0) or 0.0,
    )


def _paints_from_dict(data: Any) -> List[Paint]:
    if not isinstance(data, list):
        return []
    out: List[Paint] = []
    for item in data:
        paint = _paint_from_dict(item)
        if paint is not None:
            out.append(paint)
    return out


def _paint_from_dict(data: Any) -> Optional[Paint]:
    if not isinstance(data, dict):
        return None
    ptype = str(_pick(data, "type") or "SOLID").upper()
    paint = Paint(
        type=ptype,
        color=_color_from_dict(_pick(data, "color")),
        opacity=_as_float(_pick(data, "opacity"), 1.0) or 1.0,
        image_ref=_str_or_none(_pick(data, "image_ref", "imageRef")),
    )
    if ptype == "GRADIENT_LINEAR":
        paint.gradient_stops = _gradient_stops_from_dict(
            _pick(data, "gradient_stops", "gradientStops", "stops")
        )
        paint.gradient_angle = _as_float(
            _pick(data, "gradient_angle", "gradientAngle", "angle")
        )
    return paint


def _gradient_stops_from_dict(data: Any) -> List[GradientStop]:
    if not isinstance(data, list):
        return []
    stops: List[GradientStop] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        stops.append(
            GradientStop(
                position=_clamp01(_as_float(_pick(item, "position", "offset"), 0.0) or 0.0),
                color=_color_from_dict(_pick(item, "color")) or Color(),
            )
        )
    return stops


def _textstyle_from_dict(data: Any, fills: List[Paint]) -> TextStyle:
    data = data if isinstance(data, dict) else {}
    return TextStyle(
        font_family=_str_or_none(_pick(data, "font_family", "fontFamily")),
        font_size=_as_float(_pick(data, "font_size", "fontSize")),
        font_weight=_as_int(_pick(data, "font_weight", "fontWeight")),
        line_height_px=_as_float(
            _pick(data, "line_height_px", "lineHeightPx", "line_height", "lineHeight")
        ),
        letter_spacing=_as_float(_pick(data, "letter_spacing", "letterSpacing")),
        text_align_horizontal=_str_or_none(
            _pick(data, "text_align_horizontal", "textAlignHorizontal", "text_align", "textAlign")
        ),
        fills=list(fills),
    )


def _padding_from_dict(data: Any) -> Optional[Dict[str, float]]:
    if not isinstance(data, dict):
        return None
    keys = ("top", "right", "bottom", "left")
    if not any(key in data for key in keys):
        return None
    return {key: _as_float(data.get(key), 0.0) or 0.0 for key in keys}


def _color_from_dict(data: Any) -> Optional[Color]:
    if isinstance(data, str):
        return _color_from_hex(data)
    if not isinstance(data, dict):
        return None
    r = _as_float(_pick(data, "r", "red"))
    g = _as_float(_pick(data, "g", "green"))
    b = _as_float(_pick(data, "b", "blue"))
    a = _as_float(_pick(data, "a", "alpha"))
    if r is None and g is None and b is None:
        hex_value = _pick(data, "hex", "color")
        return _color_from_hex(hex_value) if isinstance(hex_value, str) else None
    r, g, b = r or 0.0, g or 0.0, b or 0.0
    # Heuristic: a channel >1 means the model used 0..255 instead of 0..1.
    if max(r, g, b) > 1.0:
        r, g, b = r / 255.0, g / 255.0, b / 255.0
    return Color(
        r=_clamp01(r),
        g=_clamp01(g),
        b=_clamp01(b),
        a=_clamp01(a) if a is not None else 1.0,
    )


def _color_from_hex(value: str) -> Optional[Color]:
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) not in (6, 8):
        return None
    try:
        r = int(text[0:2], 16) / 255.0
        g = int(text[2:4], 16) / 255.0
        b = int(text[4:6], 16) / 255.0
        a = int(text[6:8], 16) / 255.0 if len(text) == 8 else 1.0
    except ValueError:
        return None
    return Color(r=r, g=g, b=b, a=a)


def _str_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text or None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _clamp01(value: Any) -> float:
    number = _as_float(value, 0.0) or 0.0
    return 0.0 if number < 0 else 1.0 if number > 1 else number


def _as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _color_key(color: Color) -> str:
    return f"{round(color.r, 3)},{round(color.g, 3)},{round(color.b, 3)},{round(color.a, 3)}"


def _collect_color(tokens: DesignTokens, color: Color) -> None:
    existing = {_color_key(c) for c in tokens.colors}
    if _color_key(color) not in existing:
        tokens.colors.append(color)


def _collect_font(tokens: DesignTokens, style: TextStyle) -> None:
    key = (style.font_family, style.font_size, style.font_weight)
    existing = {(s.font_family, s.font_size, s.font_weight) for s in tokens.fonts}
    if key not in existing:
        # Store a fills-free copy so the token table stays compact.
        tokens.fonts.append(
            TextStyle(
                font_family=style.font_family,
                font_size=style.font_size,
                font_weight=style.font_weight,
                line_height_px=style.line_height_px,
                letter_spacing=style.letter_spacing,
                text_align_horizontal=style.text_align_horizontal,
            )
        )


def _collect_spacing(tokens: Optional[DesignTokens], value: Optional[float]) -> None:
    if tokens is None or value is None or value <= 0:
        return
    if value not in tokens.spacings:
        tokens.spacings.append(value)


def _clean(value: Any) -> Any:
    """Drop None / empty-list fields from the asdict() output to keep IR compact."""
    if isinstance(value, dict):
        cleaned = {}
        for key, val in value.items():
            sub = _clean(val)
            if sub is None or sub == [] or sub == {}:
                continue
            cleaned[key] = sub
        return cleaned
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value
