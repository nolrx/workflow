"""
Figma export package — a short-lived, plugin-consumable design bundle.

Export to Figma goes through a companion plugin (Figma REST is read-only), so the
platform stores the export payload (a plugin-ready Design IR) keyed by a one-time
pairing code. The user enters that code in the plugin, which fetches the payload
from the unauthenticated ``/pull`` endpoint — hence the code is high-entropy,
single-use, and expires quickly.
"""
import secrets
import uuid
from datetime import datetime, timedelta

from backend.extensions import db

# Crockford-ish base32 without easily-confused chars (no 0/1/I/O).
_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
_CODE_LENGTH = 8
EXPORT_TTL_MINUTES = 5


def generate_pairing_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


class FigmaExportPackage(db.Model):
    """A one-time, expiring export bundle fetched by the Figma plugin."""

    __tablename__ = "figma_export_packages"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    project_id = db.Column(db.String(36), db.ForeignKey("code_projects.id"), nullable=True)

    pairing_code = db.Column(db.String(16), nullable=False, unique=True, index=True)
    payload_json = db.Column(db.Text, nullable=False)  # ir_to_plugin_payload (may inline images)
    source = db.Column(db.String(30), nullable=True)  # preview_image | html
    consumed = db.Column(db.Boolean, nullable=False, default=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @classmethod
    def create(cls, *, user_id, project_id, payload_json, source):
        return cls(
            user_id=user_id,
            project_id=project_id,
            pairing_code=generate_pairing_code(),
            payload_json=payload_json,
            source=source,
            expires_at=datetime.utcnow() + timedelta(minutes=EXPORT_TTL_MINUTES),
        )

    def is_valid(self) -> bool:
        return (not self.consumed) and datetime.utcnow() < self.expires_at

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "pairing_code": self.pairing_code,
            "source": self.source,
            "expires_at": self.expires_at.isoformat() + "Z" if self.expires_at else None,
            "ttl_seconds": EXPORT_TTL_MINUTES * 60,
        }
