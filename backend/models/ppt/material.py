"""
PPT Material model - stores material images
"""
import uuid
from datetime import datetime
from backend.extensions import db


class PPTMaterial(db.Model):
    """
    PPT Material model - represents a material image
    """
    __tablename__ = "ppt_materials"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # Multi-tenant support
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    project_id = db.Column(
        db.String(36), db.ForeignKey("ppt_projects.id"), nullable=True, index=True
    )  # Can be null for global materials

    filename = db.Column(db.String(500), nullable=False)
    relative_path = db.Column(db.String(500), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # Relationships
    project = db.relationship("PPTProject", back_populates="materials")

    def to_dict(self) -> dict:
        """Convert to dictionary"""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "project_id": self.project_id,
            "filename": self.filename,
            "url": self.url,
            "relative_path": self.relative_path,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:
        return f"<PPTMaterial {self.id}: {self.filename}>"
