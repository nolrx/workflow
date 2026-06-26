"""
In-app notification model.

A lightweight, generic notification feed: every row is one notice addressed to a
single recipient (``user_id``). ``type`` drives how the frontend renders the item
(e.g. a ``team_invite`` shows inline accept/reject buttons), ``data`` carries the
type-specific payload, and ``ref_type``/``ref_id`` link the notice back to the
domain object that produced it (e.g. a ``team_invitation``) so it can be marked
read when that object is acted on. Kept deliberately reusable so future features
(deploy finished, run failed, ...) can drop in without schema churn.
"""
import uuid
from datetime import datetime

from backend.extensions import db


class Notification(db.Model):
    """A single in-app notice addressed to one user."""
    __tablename__ = "notifications"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(
        db.String(36),
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Render/route key, e.g. team_invite / code_deploy_succeeded / run_failed.
    type = db.Column(db.String(50), nullable=False)
    # Generic severity driving the dot/icon colour: info | success | warning | error.
    level = db.Column(db.String(20), default="info")
    title = db.Column(db.String(255))
    body = db.Column(db.Text)
    # Type-specific payload (team_id, team_name, role, token, inviter_name, ...).
    data = db.Column(db.JSON, default=dict)
    # Back-link to the source object so it can be marked read when acted on.
    ref_type = db.Column(db.String(50))
    ref_id = db.Column(db.String(36))

    is_read = db.Column(db.Boolean, default=False, nullable=False, index=True)
    read_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "level": self.level or "info",
            "title": self.title,
            "body": self.body,
            "data": self.data or {},
            "ref_type": self.ref_type,
            "ref_id": self.ref_id,
            "is_read": self.is_read,
            "read_at": self.read_at.isoformat() + "Z" if self.read_at else None,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
        }
