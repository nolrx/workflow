"""
Figma integration for the Code domain.

This subpackage owns the bidirectional bridge between platform artifacts and
Figma:

- ``ir``      — the canonical Design IR (a Figma-node-subset JSON) that sits
                between the platform and Figma in both directions, plus the
                converters ``figma_node_to_ir`` (import) and
                ``ir_to_plugin_payload`` (export).
- ``crypto``  — at-rest encryption for the user's Figma personal access token.

Kept dependency-free of Flask/DB so it can be imported from routes, services and
background workflow threads alike, and unit-tested in isolation.
"""
from backend.services.code.figma.ir import (
    IR_VERSION,
    Box,
    Color,
    DesignIR,
    DesignTokens,
    IRNode,
    Paint,
    TextStyle,
    figma_node_to_ir,
    image_design_ir,
    ir_to_plugin_payload,
)

__all__ = [
    "IR_VERSION",
    "Box",
    "Color",
    "DesignIR",
    "DesignTokens",
    "IRNode",
    "Paint",
    "TextStyle",
    "figma_node_to_ir",
    "image_design_ir",
    "ir_to_plugin_payload",
]
