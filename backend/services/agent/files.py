"""
Artifact file storage for agent runs.

Files live under the shared ``UPLOAD_FOLDER`` and are addressed by a path
relative to it (so the DB stores portable relative paths). Layout:

    uploads/agent_runs/{run_id}/{step_id}/{filename}
"""
from pathlib import Path

from flask import current_app
from werkzeug.utils import secure_filename


def upload_root() -> Path:
    root = Path(current_app.config.get("UPLOAD_FOLDER", "uploads"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def agent_run_dir(run_id: str, step_id: str | None = None) -> Path:
    directory = upload_root() / "agent_runs" / run_id
    if step_id:
        directory = directory / step_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def save_artifact_file(run_id: str, step_id: str | None, filename: str, data: bytes) -> str:
    """Persist artifact bytes and return the path relative to the upload root."""
    safe_name = secure_filename(filename) or "artifact.bin"
    directory = agent_run_dir(run_id, step_id or "_run")
    path = directory / safe_name
    with open(path, "wb") as file_handle:
        file_handle.write(data)
    return str(path.relative_to(upload_root()))


def artifact_abs_path(relative_path: str) -> Path:
    return upload_root() / relative_path
