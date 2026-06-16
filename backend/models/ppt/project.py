"""
PPT Project model
"""
import uuid
from datetime import datetime
from backend.extensions import db


class PPTProject(db.Model):
    """
    PPT Project model - represents a PPT project
    """
    __tablename__ = "ppt_projects"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # Multi-tenant support
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    team_id = db.Column(db.String(36), db.ForeignKey("teams.id"), nullable=True, index=True)

    # Project content
    idea_prompt = db.Column(db.Text, nullable=True)
    outline_text = db.Column(db.Text, nullable=True)  # User input outline text
    description_text = db.Column(db.Text, nullable=True)  # User input description text
    extra_requirements = db.Column(db.Text, nullable=True)  # Extra requirements for AI prompts
    creation_type = db.Column(db.String(20), nullable=False, default="idea")  # idea|outline|descriptions

    # Template settings
    template_image_path = db.Column(db.String(500), nullable=True)
    template_style = db.Column(db.Text, nullable=True)  # Style description (no template image mode)

    # Export settings
    export_extractor_method = db.Column(db.String(50), nullable=True, default="hybrid")
    export_inpaint_method = db.Column(db.String(50), nullable=True, default="hybrid")

    # Visibility
    visibility = db.Column(db.String(20), nullable=False, default="private")  # private|team|public

    # Status and timestamps
    status = db.Column(db.String(50), nullable=False, default="DRAFT")
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    pages = db.relationship(
        "PPTPage",
        back_populates="project",
        lazy="select",
        cascade="all, delete-orphan",
        order_by="PPTPage.order_index",
    )
    tasks = db.relationship(
        "PPTTask",
        back_populates="project",
        lazy="select",
        cascade="all, delete-orphan",
    )
    materials = db.relationship(
        "PPTMaterial",
        back_populates="project",
        lazy="select",
        cascade="all, delete-orphan",
    )
    reference_files = db.relationship(
        "PPTReferenceFile",
        back_populates="project",
        lazy="select",
        cascade="all, delete-orphan",
    )

    def to_dict(self, include_pages: bool = False) -> dict:
        """Convert to dictionary"""
        created_at_str = None
        if self.created_at:
            created_at_str = (
                self.created_at.isoformat() + "Z"
                if not self.created_at.tzinfo
                else self.created_at.isoformat()
            )

        updated_at_str = None
        if self.updated_at:
            updated_at_str = (
                self.updated_at.isoformat() + "Z"
                if not self.updated_at.tzinfo
                else self.updated_at.isoformat()
            )

        data = {
            "id": self.id,
            "user_id": self.user_id,
            "team_id": self.team_id,
            "idea_prompt": self.idea_prompt,
            "outline_text": self.outline_text,
            "description_text": self.description_text,
            "extra_requirements": self.extra_requirements,
            "creation_type": self.creation_type,
            "template_image_url": (
                f"/api/ppt/files/{self.id}/template/{self.template_image_path.split('/')[-1]}"
                if self.template_image_path
                else None
            ),
            "template_style": self.template_style,
            "export_extractor_method": self.export_extractor_method or "hybrid",
            "export_inpaint_method": self.export_inpaint_method or "hybrid",
            "visibility": self.visibility,
            "status": self.status,
            "created_at": created_at_str,
            "updated_at": updated_at_str,
        }

        if include_pages:
            data["pages"] = [page.to_dict() for page in self.pages]

        return data

    def __repr__(self) -> str:
        return f"<PPTProject {self.id}: {self.status}>"
