"""
Build Figma-plugin-consumable export payloads from Code-domain artifacts.

Phase 2 supports the deterministic preview-image path (zero model calls): the
rendered preview PNG becomes a single image-filled frame the plugin drops onto
the canvas at 100% fidelity. The HTML -> IR path (AI-generated layer tree) is
added in a later phase.
"""
import base64
import logging
from io import BytesIO
from pathlib import Path
from typing import Optional, Tuple

from backend.services.code.figma.ir import IR_VERSION, image_design_ir, ir_to_plugin_payload

logger = logging.getLogger(__name__)

# Guard against an oversized inline image blowing up the export payload row.
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_PROMPT_DIR = Path(__file__).parent.parent.parent / "prompts" / "code"
_MAX_HTML_CHARS = 120_000


class ExportError(Exception):
    """A user-facing export failure (bad source / missing image)."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _decode_data_url(data_url: str) -> Tuple[bytes, str]:
    """Return (bytes, mime) from a ``data:<mime>;base64,<payload>`` URL."""
    if not data_url or not data_url.startswith("data:"):
        raise ExportError("VALIDATION_ERROR", "该预览图无法导出（不是内联图片）")
    try:
        header, payload = data_url.split(",", 1)
        mime = header[5:].split(";", 1)[0] or "image/png"
        raw = base64.b64decode(payload)
    except Exception as exc:  # noqa: BLE001
        raise ExportError("VALIDATION_ERROR", "预览图数据无效") from exc
    if len(raw) > _MAX_IMAGE_BYTES:
        raise ExportError("VALIDATION_ERROR", "预览图过大，无法导出到 Figma")
    return raw, mime


def _image_size(raw: bytes) -> Tuple[int, int]:
    """Best-effort (width, height); falls back to a square if PIL can't read it."""
    try:
        from PIL import Image

        with Image.open(BytesIO(raw)) as img:
            return img.width, img.height
    except Exception:  # noqa: BLE001
        return 1024, 1024


def select_preview_data_url(project, preview_id: Optional[str]) -> str:
    """Pick the data URL to export from a project's preview images.

    Priority: the explicitly requested preview id -> the confirmed preview ->
    the first generated preview.
    """
    previews = project.get_preview_images()
    if preview_id:
        for item in previews:
            if item.get("id") == preview_id:
                url = item.get("url")
                if url:
                    return url
        raise ExportError("NOT_FOUND", "找不到指定的预览图", 404)
    if project.confirmed_preview_url:
        return project.confirmed_preview_url
    if previews and previews[0].get("url"):
        return previews[0]["url"]
    raise ExportError("NOT_FOUND", "项目还没有可导出的预览图", 404)


def build_preview_export_payload(project, preview_id: Optional[str]) -> dict:
    """Build a plugin payload from a project's preview image (deterministic)."""
    data_url = select_preview_data_url(project, preview_id)
    raw, _mime = _decode_data_url(data_url)
    width, height = _image_size(raw)
    design = image_design_ir(
        name=project.title or "Preview",
        image_data_url=data_url,
        width=float(width),
        height=float(height),
    )
    return ir_to_plugin_payload(design)


def latest_frontend_html(project_id: str) -> Optional[str]:
    """Return the most recent generated frontend HTML for a project, if any."""
    from backend.models.agent import AgentArtifact

    artifact = (
        AgentArtifact.query.filter_by(
            domain_ref_type="code_frontend_html", domain_ref_id=project_id
        )
        .order_by(AgentArtifact.created_at.desc())
        .first()
    )
    return artifact.content_text if artifact else None


# Reject an oversized assembled payload row (slice payloads inline N images as
# base64). The slice workflow already bounds this; this is a final backstop.
_MAX_PAYLOAD_BYTES = 32 * 1024 * 1024


def build_sliced_export_payload(project, run_id: Optional[str] = None) -> dict:
    """Return the plugin payload produced by a ``code_figma_slice_generation`` run.

    Deterministic (no model call) — the editable layer tree was already built by
    the slice workflow and stored as a ``code_figma_slice_payload`` artifact. We
    just fetch the latest one for this project (optionally pinned to a specific
    ``run_id`` to avoid racing a concurrent/older run) and hand it back.
    """
    from backend.models.agent import AgentArtifact

    query = AgentArtifact.query.filter_by(
        domain_ref_type="code_figma_slice_payload", domain_ref_id=project.id
    )
    if run_id:
        query = query.filter(AgentArtifact.run_id == run_id)
    artifact = query.order_by(AgentArtifact.created_at.desc()).first()
    if not artifact or not artifact.content_json_raw:
        raise ExportError(
            "NOT_FOUND", "项目还没有切片导出包，请先运行「智能切片」分析", 404
        )

    if len(artifact.content_json_raw.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
        raise ExportError("VALIDATION_ERROR", "切片导出包过大，无法导出到 Figma", 400)

    payload = artifact.get_content_json()
    if not isinstance(payload, dict) or not isinstance(payload.get("root"), dict):
        raise ExportError("SERVER_ERROR", "切片导出包结构无效", 500)

    payload.setdefault("ir_version", IR_VERSION)
    payload["source"] = "sliced"
    payload.setdefault("name", project.title or "Design")
    payload.setdefault("images", {})
    return payload


def build_html_export_payload(project, *, on_model_call=None) -> dict:
    """Convert a project's generated HTML into a plugin layer-tree payload (AI).

    Unlike the preview path this requires a model call (no headless browser is
    available to measure the DOM), so the caller charges for it.
    """
    from backend.services.code.frontend_build_service import (
        FrontendBuildService,
        get_frontend_build_service,
    )

    html = latest_frontend_html(project.id)
    if not html:
        raise ExportError("NOT_FOUND", "项目还没有生成的前端 HTML，无法导出", 404)

    template = (_PROMPT_DIR / "html_to_figma_ir_prompt.txt").read_text(encoding="utf-8")
    prompt = template.replace("[[HTML]]", html[:_MAX_HTML_CHARS])

    service = get_frontend_build_service()
    text, success, error = service._call_model(prompt, on_model_call)
    if not success:
        raise ExportError("SERVER_ERROR", f"HTML 转 Figma 失败：{error or '模型不可用'}", 502)

    payload = FrontendBuildService._extract_json(text or "")
    if not payload or not isinstance(payload.get("root"), dict):
        raise ExportError("SERVER_ERROR", "模型未返回有效的 Figma 图层结构", 502)

    # Normalise the envelope so the plugin always gets a complete payload.
    payload.setdefault("ir_version", IR_VERSION)
    payload["source"] = "html"
    payload.setdefault("name", project.title or "Design")
    payload.setdefault("images", {})
    return payload
