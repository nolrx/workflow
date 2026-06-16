"""
PPT User Template model - stores user-uploaded templates
"""
import uuid
from datetime import datetime
from backend.extensions import db


class PPTUserTemplate(db.Model):
    """
    PPT User Template model - represents a user-uploaded template
    """
    __tablename__ = "ppt_user_templates"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # Multi-tenant support
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    team_id = db.Column(db.String(36), db.ForeignKey("teams.id"), nullable=True, index=True)

    name = db.Column(db.String(200), nullable=True)  # Optional template name
    file_path = db.Column(db.String(500), nullable=False)
    file_size = db.Column(db.Integer, nullable=True)
    visibility = db.Column(db.String(20), nullable=False, default="private")  # private|team|public
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    def to_dict(self) -> dict:
        """Convert to dictionary"""
        return {
            "template_id": self.id,
            "user_id": self.user_id,
            "team_id": self.team_id,
            "name": self.name,
            "template_image_url": f"/api/ppt/files/user-templates/{self.id}/{self.file_path.split('/')[-1]}",
            "visibility": self.visibility,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:
        return f"<PPTUserTemplate {self.id}: {self.name or 'Unnamed'}>"
