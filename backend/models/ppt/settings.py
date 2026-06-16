"""
PPT Settings model - stores per-user PPT generation settings
"""
import uuid
from datetime import datetime
from backend.extensions import db


class PPTSettings(db.Model):
    """
    PPT Settings model - represents user-specific PPT generation settings
    """
    __tablename__ = "ppt_settings"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # User association (one settings per user)
    user_id = db.Column(
        db.String(36), db.ForeignKey("users.id"), nullable=False, unique=True, index=True
    )

    # AI Provider Configuration
    ai_provider = db.Column(db.String(50), nullable=True)  # gemini, openai
    api_base_url = db.Column(db.String(500), nullable=True)
    api_key = db.Column(db.String(500), nullable=True)  # Encrypted in production
    text_model = db.Column(db.String(100), nullable=True)
    image_model = db.Column(db.String(100), nullable=True)

    # Image Generation Settings
    image_resolution = db.Column(db.String(10), nullable=False, default="2K")  # 1K, 2K, 4K
    image_aspect_ratio = db.Column(db.String(10), nullable=False, default="16:9")

    # Worker Settings (for parallel generation)
    max_description_workers = db.Column(db.Integer, nullable=False, default=3)
    max_image_workers = db.Column(db.Integer, nullable=False, default=2)

    # Output Language
    output_language = db.Column(db.String(10), nullable=False, default="zh")  # zh, en, ja, auto

    # Timestamps
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # Relationship
    user = db.relationship("User", backref=db.backref("ppt_settings", uselist=False))

    @classmethod
    def get_or_create(cls, user_id: str) -> "PPTSettings":
        """
        Get settings for a user, creating default settings if none exist.

        Args:
            user_id: User ID

        Returns:
            PPTSettings instance
        """
        settings = cls.query.filter_by(user_id=user_id).first()
        if not settings:
            settings = cls(user_id=user_id)
            db.session.add(settings)
            db.session.commit()
        return settings

    def to_dict(self, include_sensitive: bool = False) -> dict:
        """
        Convert to dictionary

        Args:
            include_sensitive: Whether to include API key (masked)
        """
        result = {
            "id": self.id,
            "user_id": self.user_id,
            "ai_provider": self.ai_provider,
            "api_base_url": self.api_base_url,
            "text_model": self.text_model,
            "image_model": self.image_model,
            "image_resolution": self.image_resolution,
            "image_aspect_ratio": self.image_aspect_ratio,
            "max_description_workers": self.max_description_workers,
            "max_image_workers": self.max_image_workers,
            "output_language": self.output_language,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

        if include_sensitive:
            # Mask API key for security
            if self.api_key:
                if len(self.api_key) > 8:
                    result["api_key"] = self.api_key[:4] + "****" + self.api_key[-4:]
                else:
                    result["api_key"] = "****"
            else:
                result["api_key"] = None
            result["has_api_key"] = bool(self.api_key)
        else:
            result["has_api_key"] = bool(self.api_key)

        return result

    def __repr__(self) -> str:
        return f"<PPTSettings {self.id}: user={self.user_id}>"
