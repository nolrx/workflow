"""
PPT Reference File model - stores uploaded reference files and their parsed content
"""
import re
import uuid
from datetime import datetime
from backend.extensions import db


class PPTReferenceFile(db.Model):
    """
    PPT Reference File model - represents an uploaded reference file
    """
    __tablename__ = "ppt_reference_files"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # Multi-tenant support
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    project_id = db.Column(
        db.String(36), db.ForeignKey("ppt_projects.id"), nullable=True, index=True
    )  # Can be null for global files

    filename = db.Column(db.String(500), nullable=False)
    file_path = db.Column(db.String(500), nullable=False)
    file_size = db.Column(db.Integer, nullable=False)  # File size in bytes
    file_type = db.Column(db.String(50), nullable=False)  # pdf, docx, pptx, etc.
    parse_status = db.Column(
        db.String(50), nullable=False, default="pending"
    )  # pending|parsing|completed|failed
    markdown_content = db.Column(db.Text, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    mineru_batch_id = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # Relationships
    project = db.relationship("PPTProject", back_populates="reference_files")

    def to_dict(
        self, include_content: bool = True, include_failed_count: bool = False
    ) -> dict:
        """
        Convert to dictionary

        Args:
            include_content: Whether to include markdown_content (can be large)
            include_failed_count: Whether to calculate failed image count
        """
        result = {
            "id": self.id,
            "user_id": self.user_id,
            "project_id": self.project_id,
            "filename": self.filename,
            "file_size": self.file_size,
            "file_type": self.file_type,
            "parse_status": self.parse_status,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

        if include_content:
            result["markdown_content"] = self.markdown_content

        if include_failed_count and self.parse_status == "completed":
            result["image_caption_failed_count"] = self.count_failed_image_captions()

        return result

    def count_failed_image_captions(self) -> int:
        """
        Count images in markdown that don't have alt text

        Returns:
            Number of images without captions
        """
        if not self.markdown_content:
            return 0

        pattern = r"!\[(.*?)\]\([^\)]+\)"
        matches = re.findall(pattern, self.markdown_content)
        failed_count = sum(1 for alt_text in matches if not alt_text.strip())
        return failed_count

    def __repr__(self) -> str:
        return f"<PPTReferenceFile {self.id}: {self.filename} ({self.parse_status})>"
