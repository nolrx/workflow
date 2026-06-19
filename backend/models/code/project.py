"""
Code creation project models.
"""
import json
import uuid
from datetime import datetime

from backend.extensions import db


class CodeProjectStatus:
    """Code project workflow status constants."""

    REQUIREMENT_READY = "requirement_ready"
    FLOW_READY = "flow_ready"
    DOCUMENTS_READY = "documents_ready"
    STYLE_READY = "style_ready"
    PREVIEW_READY = "preview_ready"
    UI_CONFIRMED = "ui_confirmed"


class CodeProject(db.Model):
    """Software creation project with editable workflow artifacts."""

    __tablename__ = "code_projects"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    team_id = db.Column(db.String(36), db.ForeignKey("teams.id"), nullable=True, index=True)

    title = db.Column(db.String(300), nullable=False)
    requirement_input = db.Column(db.Text, nullable=False)
    requirements_doc = db.Column(db.Text, nullable=True)
    development_flow = db.Column(db.Text, nullable=True)
    style_prompt = db.Column(db.Text, nullable=True)
    ui_baseline_prompt = db.Column(db.Text, nullable=True)
    confirmed_preview_url = db.Column(db.Text, nullable=True)

    selected_style_ids_raw = db.Column(db.Text, nullable=True)
    preview_images_raw = db.Column(db.Text, nullable=True)

    status = db.Column(db.String(30), nullable=False, default=CodeProjectStatus.REQUIREMENT_READY)
    visibility = db.Column(db.String(20), default="private")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = db.relationship("User", backref=db.backref("code_projects", lazy="dynamic"))
    team = db.relationship("Team", backref=db.backref("code_projects", lazy="dynamic"))
    documents = db.relationship(
        "CodeDocument",
        backref="project",
        lazy="dynamic",
        cascade="all, delete-orphan",
        order_by="CodeDocument.order_index",
    )
    stage_versions = db.relationship(
        "CodeStageVersion",
        backref="project",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )

    def get_selected_style_ids(self) -> list[str]:
        """Return selected UI style ids."""
        if not self.selected_style_ids_raw:
            return []
        try:
            value = json.loads(self.selected_style_ids_raw)
            return value if isinstance(value, list) else []
        except json.JSONDecodeError:
            return []

    def set_selected_style_ids(self, style_ids: list[str]) -> None:
        """Persist selected UI style ids."""
        self.selected_style_ids_raw = json.dumps(style_ids or [], ensure_ascii=False)

    def get_preview_images(self) -> list[dict]:
        """Return generated preview image metadata."""
        if not self.preview_images_raw:
            return []
        try:
            value = json.loads(self.preview_images_raw)
            return value if isinstance(value, list) else []
        except json.JSONDecodeError:
            return []

    def set_preview_images(self, images: list[dict]) -> None:
        """Persist generated preview image metadata."""
        self.preview_images_raw = json.dumps(images or [], ensure_ascii=False)

    def to_dict(self, include_documents: bool = True) -> dict:
        """Convert the project to an API dictionary."""
        data = {
            "id": self.id,
            "user_id": self.user_id,
            "team_id": self.team_id,
            "title": self.title,
            "requirement_input": self.requirement_input,
            "requirements_doc": self.requirements_doc,
            "development_flow": self.development_flow,
            "style_prompt": self.style_prompt,
            "ui_baseline_prompt": self.ui_baseline_prompt,
            "confirmed_preview_url": self.confirmed_preview_url,
            "selected_style_ids": self.get_selected_style_ids(),
            "preview_images": self.get_preview_images(),
            "status": self.status,
            "visibility": self.visibility,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "updated_at": self.updated_at.isoformat() + "Z" if self.updated_at else None,
        }
        if include_documents:
            data["documents"] = [document.to_dict() for document in self.documents.all()]
        return data

    def to_list_dict(self) -> dict:
        """Convert the project to a compact list dictionary."""
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "updated_at": self.updated_at.isoformat() + "Z" if self.updated_at else None,
        }


class CodeDocument(db.Model):
    """Editable document produced by the software creation workflow."""

    __tablename__ = "code_documents"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(
        db.String(36), db.ForeignKey("code_projects.id"), nullable=False, index=True
    )
    document_type = db.Column(db.String(80), nullable=False)
    title = db.Column(db.String(300), nullable=False)
    content = db.Column(db.Text, nullable=False)
    prompt_expert = db.Column(db.Text, nullable=False)
    order_index = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self) -> dict:
        """Convert the document to an API dictionary."""
        return {
            "id": self.id,
            "project_id": self.project_id,
            "document_type": self.document_type,
            "title": self.title,
            "content": self.content,
            "prompt_expert": self.prompt_expert,
            "order_index": self.order_index,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "updated_at": self.updated_at.isoformat() + "Z" if self.updated_at else None,
        }
