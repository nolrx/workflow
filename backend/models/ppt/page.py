"""
PPT Page model
"""
import uuid
import json
from datetime import datetime
from backend.extensions import db


class PPTPage(db.Model):
    """
    PPT Page model - represents a single PPT page/slide
    """
    __tablename__ = "ppt_pages"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(
        db.String(36), db.ForeignKey("ppt_projects.id"), nullable=False, index=True
    )
    order_index = db.Column(db.Integer, nullable=False)
    part = db.Column(db.String(200), nullable=True)  # Optional section name
    outline_content = db.Column(db.Text, nullable=True)  # JSON string
    description_content = db.Column(db.Text, nullable=True)  # JSON string
    generated_image_path = db.Column(db.String(500), nullable=True)
    status = db.Column(db.String(50), nullable=False, default="DRAFT")
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # Relationships
    project = db.relationship("PPTProject", back_populates="pages")
    image_versions = db.relationship(
        "PPTPageImageVersion",
        back_populates="page",
        lazy="dynamic",
        cascade="all, delete-orphan",
        order_by="PPTPageImageVersion.version_number.desc()",
    )

    def get_outline_content(self) -> dict | None:
        """Parse outline_content from JSON string"""
        if self.outline_content:
            try:
                return json.loads(self.outline_content)
            except json.JSONDecodeError:
                return None
        return None

    def set_outline_content(self, data: dict | None) -> None:
        """Set outline_content as JSON string"""
        if data:
            self.outline_content = json.dumps(data, ensure_ascii=False)
        else:
            self.outline_content = None

    def get_description_content(self) -> dict | None:
        """Parse description_content from JSON string"""
        if self.description_content:
            try:
                return json.loads(self.description_content)
            except json.JSONDecodeError:
                return None
        return None

    def set_description_content(self, data: dict | None) -> None:
        """Set description_content as JSON string"""
        if data:
            self.description_content = json.dumps(data, ensure_ascii=False)
        else:
            self.description_content = None

    def to_dict(self, include_versions: bool = False) -> dict:
        """Convert to dictionary"""
        data = {
            "page_id": self.id,
            "project_id": self.project_id,
            "order_index": self.order_index,
            "part": self.part,
            "outline_content": self.get_outline_content(),
            "description_content": self.get_description_content(),
            "generated_image_url": (
                f"/api/ppt/files/{self.project_id}/pages/{self.generated_image_path.split('/')[-1]}"
                if self.generated_image_path
                else None
            ),
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

        if include_versions:
            data["image_versions"] = [v.to_dict() for v in self.image_versions.all()]

        return data

    def __repr__(self) -> str:
        return f"<PPTPage {self.id}: {self.order_index} - {self.status}>"
