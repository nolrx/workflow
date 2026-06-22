"""
On-disk storage for attached Figma design render images.

Render PNGs are large, so they live on the filesystem under the shared upload
root (NOT inlined in the DB). Layout:

    uploads/figma_designs/{project_id}/{frame}.png

``CodeFigmaDesign`` stores only each frame's ``render_filename``; the project
generation reads the bytes back via ``render_path`` and copies them into the
container workdir.
"""
import shutil
from pathlib import Path

from flask import current_app
from werkzeug.utils import secure_filename


def upload_root() -> Path:
    root = Path(current_app.config.get("UPLOAD_FOLDER", "uploads"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def figma_design_dir(project_id: str) -> Path:
    directory = upload_root() / "figma_designs" / project_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def clear_design_dir(project_id: str) -> None:
    """Drop a project's render images (called before a re-import replaces them)."""
    directory = upload_root() / "figma_designs" / project_id
    if directory.exists():
        shutil.rmtree(directory, ignore_errors=True)


def save_render(project_id: str, node_id: str, data: bytes) -> str:
    """Persist one frame's render PNG; returns the filename (not the full path)."""
    safe = secure_filename(node_id.replace(":", "_")) or "frame"
    filename = f"{safe}.png"
    path = figma_design_dir(project_id) / filename
    with open(path, "wb") as handle:
        handle.write(data)
    return filename


def render_path(project_id: str, filename: str) -> Path:
    """Absolute path to a stored render image."""
    return figma_design_dir(project_id) / filename
