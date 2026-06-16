"""
Team models for collaboration
"""
import uuid
import secrets
from datetime import datetime, timedelta
from backend.extensions import db


class Team(db.Model):
    """Team/Workspace model."""
    __tablename__ = "teams"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = db.Column(db.String(100), nullable=False)
    slug = db.Column(db.String(100), unique=True, nullable=False)
    owner_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False)

    # Settings
    settings = db.Column(db.JSON, default=dict)

    # Subscription
    plan_id = db.Column(db.String(36), db.ForeignKey("plans.id"))
    stripe_subscription_id = db.Column(db.String(100))

    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    owner = db.relationship("User", backref="owned_teams", foreign_keys=[owner_id])
    members = db.relationship("TeamMember", back_populates="team", cascade="all, delete-orphan")
    plan = db.relationship("Plan", backref="teams")
    credit_balance = db.relationship("TeamCreditBalance", back_populates="team", uselist=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "slug": self.slug,
            "owner_id": self.owner_id,
            "settings": self.settings,
            "plan": self.plan.to_dict() if self.plan else None,
            "member_count": len(self.members),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class TeamMember(db.Model):
    """Team membership model."""
    __tablename__ = "team_members"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    team_id = db.Column(db.String(36), db.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    user_id = db.Column(db.String(36), db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="member")  # owner, admin, member, viewer

    # Member-specific settings
    can_create_projects = db.Column(db.Boolean, default=True)
    can_use_credits = db.Column(db.Boolean, default=True)

    joined_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    team = db.relationship("Team", back_populates="members")
    user = db.relationship("User", back_populates="team_memberships")

    __table_args__ = (
        db.UniqueConstraint("team_id", "user_id", name="uq_team_member"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "team_id": self.team_id,
            "user_id": self.user_id,
            "user": self.user.to_dict(include_email=False) if self.user else None,
            "role": self.role,
            "can_create_projects": self.can_create_projects,
            "can_use_credits": self.can_use_credits,
            "joined_at": self.joined_at.isoformat() if self.joined_at else None,
        }


class TeamInvitation(db.Model):
    """Team invitation model."""
    __tablename__ = "team_invitations"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    team_id = db.Column(db.String(36), db.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    email = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default="member")
    token = db.Column(db.String(100), unique=True, nullable=False, default=lambda: secrets.token_urlsafe(32))
    invited_by = db.Column(db.String(36), db.ForeignKey("users.id"))

    expires_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.utcnow() + timedelta(days=7))
    accepted_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    team = db.relationship("Team", backref="invitations")
    inviter = db.relationship("User", backref="sent_invitations")

    @property
    def is_expired(self) -> bool:
        return datetime.utcnow() > self.expires_at

    @property
    def is_accepted(self) -> bool:
        return self.accepted_at is not None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "team_id": self.team_id,
            "team": self.team.to_dict() if self.team else None,
            "email": self.email,
            "role": self.role,
            "is_expired": self.is_expired,
            "is_accepted": self.is_accepted,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
