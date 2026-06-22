"""
Per-stage version history for Code projects.

Every stage product (requirements / flow / documents / style / preview) gets an
append-only version trail. Each generation or manual edit records a new row whose
``version_number`` auto-increments within a ``(project_id, stage)`` pair, and at
most one row per pair carries ``is_current = True`` (the version whose content is
materialized on the live ``CodeProject``). This versioning lets users review or
roll back any stage.
"""
import json
import uuid
from datetime import datetime

from backend.extensions import db


class CodeStage:
    """Versionable Code project stage keys (one trail per stage)."""

    REQUIREMENTS = "requirements"
    FLOW = "flow"
    DOCUMENTS = "documents"
    STYLE = "style"
    PREVIEW = "preview"

    ALL = (REQUIREMENTS, FLOW, DOCUMENTS, STYLE, PREVIEW)


class CodeStageVersionSource:
    """How a version came to exist (shown as a badge in the history UI)."""

    GENERATED = "generated"  # produced by an AI generation step
    MANUAL_EDIT = "manual_edit"  # saved after a user edit
    PARTIAL_REVISION = "partial_revision"  # AI rewrite of a user-selected span
    ROLLBACK = "rollback"  # created by restoring an earlier version
    IMPORT = "import"  # lazily seeded baseline for a pre-existing project


class CodeStageVersion(db.Model):
    """A single historical version of one Code project stage's product."""

    __tablename__ = "code_stage_versions"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(
        db.String(36), db.ForeignKey("code_projects.id"), nullable=False, index=True
    )
    stage = db.Column(db.String(40), nullable=False, index=True)
    version_number = db.Column(db.Integer, nullable=False)
    is_current = db.Column(db.Boolean, nullable=False, default=False)
    source = db.Column(db.String(20), nullable=False, default=CodeStageVersionSource.GENERATED)

    # Text-primary stages (requirements/flow/style) use content_text; structured
    # stages (documents/preview) use content_json_raw. style also keeps its
    # selected_style_ids in content_json_raw alongside the text.
    content_text = db.Column(db.Text, nullable=True)
    content_json_raw = db.Column(db.Text, nullable=True)
    summary = db.Column(db.String(500), nullable=True)

    # Provenance: which agent run/step produced it (null for manual edits).
    run_id = db.Column(db.String(36), nullable=True, index=True)
    step_id = db.Column(db.String(36), nullable=True)
    note = db.Column(db.String(300), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def get_content_json(self) -> dict | list | None:
        """Return the structured content payload, or None."""
        if not self.content_json_raw:
            return None
        try:
            return json.loads(self.content_json_raw)
        except (json.JSONDecodeError, TypeError):
            return None

    def set_content_json(self, data) -> None:
        """Persist the structured content payload (None clears it)."""
        self.content_json_raw = (
            json.dumps(data, ensure_ascii=False) if data is not None else None
        )

    def to_dict(self, include_content: bool = False) -> dict:
        """Convert to an API dictionary; content is omitted from list views."""
        data = {
            "id": self.id,
            "project_id": self.project_id,
            "stage": self.stage,
            "version_number": self.version_number,
            "is_current": self.is_current,
            "source": self.source,
            "summary": self.summary,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "note": self.note,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
        }
        if include_content:
            data["content_text"] = self.content_text
            data["content_json"] = self.get_content_json()
        return data
