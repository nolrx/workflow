"""
Agent artifact model.

Artifacts are the concrete outputs a run produces: markdown documents, JSON
plans, generated images / preview thumbnails, etc. Text content is stored
inline for instant display; on-disk copies (``storage_path``) back the file
download endpoint. ``domain_ref_*`` links an artifact back to the business row
it ultimately produced (e.g. a CodeDocument).
"""
import json
import uuid
from datetime import datetime

from backend.extensions import db


class AgentArtifactType:
    """Artifact type constants."""

    MARKDOWN = "markdown"
    JSON = "json"
    TEXT = "text"
    IMAGE = "image"


class AgentArtifact(db.Model):
    """A concrete output produced by a run."""

    __tablename__ = "agent_artifacts"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id = db.Column(
        db.String(36), db.ForeignKey("agent_runs.id"), nullable=False, index=True
    )
    step_id = db.Column(db.String(36), nullable=True, index=True)

    artifact_type = db.Column(db.String(40), nullable=False, default=AgentArtifactType.TEXT)
    title = db.Column(db.String(300), nullable=False)
    filename = db.Column(db.String(300), nullable=True)
    mime_type = db.Column(db.String(100), nullable=True)

    storage_path = db.Column(db.String(500), nullable=True)  # relative to UPLOAD_FOLDER
    preview_url = db.Column(db.String(1000), nullable=True)  # external/data url or file route

    content_text = db.Column(db.Text, nullable=True)
    content_json_raw = db.Column(db.Text, nullable=True)

    domain_ref_type = db.Column(db.String(40), nullable=True)
    domain_ref_id = db.Column(db.String(36), nullable=True)

    version = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def get_content_json(self):
        if not self.content_json_raw:
            return None
        try:
            return json.loads(self.content_json_raw)
        except (json.JSONDecodeError, TypeError):
            return None

    def set_content_json(self, data) -> None:
        self.content_json_raw = (
            json.dumps(data, ensure_ascii=False) if data is not None else None
        )

    def to_dict(self, include_content: bool = True) -> dict:
        data = {
            "id": self.id,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "artifact_type": self.artifact_type,
            "title": self.title,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "preview_url": self.preview_url,
            "file_url": f"/api/agent/artifacts/{self.id}/file" if self.storage_path else None,
            "domain_ref_type": self.domain_ref_type,
            "domain_ref_id": self.domain_ref_id,
            "version": self.version,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
        }
        if include_content:
            data["content_text"] = self.content_text
            data["content_json"] = self.get_content_json()
        return data
