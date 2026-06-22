"""
Figma credential model — stores a user's Figma personal access token (PAT).

The PAT is stored encrypted at rest (see ``backend.services.code.figma.crypto``).
One credential per user (UPSERT on save). ``to_dict()`` deliberately never
returns the plaintext or the ciphertext — only a masked ``token_last4`` for UI.
"""
import uuid
from datetime import datetime

from backend.extensions import db


class FigmaCredential(db.Model):
    """A user's encrypted Figma personal access token."""

    __tablename__ = "figma_credentials"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    team_id = db.Column(db.String(36), db.ForeignKey("teams.id"), nullable=True, index=True)

    token_encrypted = db.Column(db.Text, nullable=False)
    token_last4 = db.Column(db.String(8), nullable=True)
    label = db.Column(db.String(120), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # One stored token per user.
    __table_args__ = (db.UniqueConstraint("user_id", name="uq_figma_cred_user"),)

    def to_dict(self) -> dict:
        """API view — never exposes the token itself, only a masked tail."""
        return {
            "id": self.id,
            "has_token": bool(self.token_encrypted),
            "token_last4": self.token_last4,
            "label": self.label,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "updated_at": self.updated_at.isoformat() + "Z" if self.updated_at else None,
        }
