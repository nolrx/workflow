"""
Attach a whole Figma file (all top-level frames) to a Code project.

No LLM: pulls the file's node tree, enumerates every top-level frame across all
canvases, renders each to a PNG (one batched ``get_image_urls`` call), converts
each to a compact Design IR, stores the renders on disk and the IR text in the
DB, and UPSERTs one ``CodeFigmaDesign`` per project. The multi-file project
generation later feeds these (render images + IR) into the containerized build.
"""
import logging
import os

from backend.extensions import db
from backend.models.code import CodeFigmaDesign
from backend.services.code.figma import storage
from backend.services.code.figma.ir import figma_node_to_ir
from backend.services.code.figma_service import FigmaError, FigmaService, parse_figma_url

logger = logging.getLogger(__name__)

# Top-level node types we treat as a "screen" worth its own page/route.
_FRAME_TYPES = {"FRAME", "COMPONENT", "COMPONENT_SET", "SECTION"}
# Per-frame IR text budget (visual detail comes from the rendered image, which the
# build agent reads on demand — the prompt only needs a compact token summary).
_PER_FRAME_IR_CHARS = 4000


def _max_frames() -> int:
    try:
        return max(1, int(os.getenv("FIGMA_MAX_FRAMES", "24")))
    except (TypeError, ValueError):
        return 24


def _enumerate_frames(document: dict) -> list[dict]:
    """All top-level frames across every canvas, in document order."""
    frames: list[dict] = []
    for canvas in (document or {}).get("children") or []:
        for node in (canvas or {}).get("children") or []:
            if isinstance(node, dict) and node.get("type") in _FRAME_TYPES:
                frames.append(node)
    return frames


def attach_design(project, token: str, figma_url: str) -> CodeFigmaDesign:
    """Fetch + render + store a Figma file's frames; UPSERT the project's design."""
    file_key, _node_id = parse_figma_url(figma_url)
    figma = FigmaService(token)
    file_data = figma.get_file(file_key)
    document = file_data.get("document") or {}
    file_name = file_data.get("name") or file_key

    frame_nodes = _enumerate_frames(document)
    if not frame_nodes:
        raise FigmaError(404, "NOT_FOUND", "该 Figma 文件没有可用的画板(frame)")
    frame_nodes = frame_nodes[: _max_frames()]

    frame_ids = [n.get("id") for n in frame_nodes if n.get("id")]
    # One batched render call; the signed URLs are short-lived, so download now.
    urls = figma.get_image_urls(file_key, frame_ids, scale=2.0, fmt="png")

    storage.clear_design_dir(project.id)  # replace any previously attached design
    frames: list[dict] = []
    for order, node in enumerate(frame_nodes):
        node_id = node.get("id")
        if not node_id:
            continue
        ir = figma_node_to_ir(node, file_name=node.get("name") or "")
        render_filename = None
        url = urls.get(node_id)
        if url:
            try:
                render_filename = storage.save_render(
                    project.id, node_id, FigmaService.download_image(url)
                )
            except FigmaError:
                logger.warning("Figma render download failed for node %s", node_id)
        box = node.get("absoluteBoundingBox") or {}
        frames.append(
            {
                "node_id": node_id,
                "name": node.get("name") or node_id,
                "order": order,
                "width": box.get("width"),
                "height": box.get("height"),
                "ir_text": ir.to_prompt_text(max_chars=_PER_FRAME_IR_CHARS),
                "render_filename": render_filename,
            }
        )

    design = CodeFigmaDesign.query.filter_by(project_id=project.id).first()
    if design is None:
        design = CodeFigmaDesign(project_id=project.id)
        db.session.add(design)
    design.user_id = project.user_id
    design.team_id = project.team_id
    design.file_key = file_key
    design.file_name = file_name
    design.source_url = figma_url
    design.set_frames(frames)
    db.session.commit()
    return design
