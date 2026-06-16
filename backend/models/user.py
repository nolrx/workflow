"""
User and Plan models
"""
import uuid
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from backend.extensions import db


class Plan(db.Model):
    """Subscription plan model."""
    __tablename__ = "plans"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = db.Column(db.String(50), nullable=False, unique=True)  # free, pro, team
    display_name = db.Column(db.String(100))
    monthly_credits = db.Column(db.Integer, default=0)
    price_monthly = db.Column(db.Integer, default=0)  # in cents
    price_yearly = db.Column(db.Integer, default=0)  # in cents
    features = db.Column(db.JSON, default=dict)  # {"max_projects": 10, "watermark": false}
    stripe_price_id_monthly = db.Column(db.String(100))
    stripe_price_id_yearly = db.Column(db.String(100))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "display_name": self.display_name,
            "monthly_credits": self.monthly_credits,
            "price_monthly": self.price_monthly,
            "price_yearly": self.price_yearly,
            "features": self.features,
        }


class User(db.Model):
    """User model."""
    __tablename__ = "users"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=True)  # Nullable for OAuth users
    display_name = db.Column(db.String(100))
    avatar_url = db.Column(db.String(500))

    # Status
    is_active = db.Column(db.Boolean, default=True)
    is_verified = db.Column(db.Boolean, default=False)
    role = db.Column(db.String(20), default="user")  # user, pro, admin

    # OAuth
    oauth_provider = db.Column(db.String(50))  # google, github
    oauth_id = db.Column(db.String(255))

    # Subscription
    plan_id = db.Column(db.String(36), db.ForeignKey("plans.id"), nullable=True)
    stripe_customer_id = db.Column(db.String(100))

    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login_at = db.Column(db.DateTime)

    # Relationships
    plan = db.relationship("Plan", backref="users")
    credit_balance = db.relationship("CreditBalance", back_populates="user", uselist=False)
    team_memberships = db.relationship("TeamMember", back_populates="user")

    def set_password(self, password: str) -> None:
        """Hash and set the password."""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        """Check if the password is correct."""
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, password)

    def to_dict(self, include_email: bool = True) -> dict:
        data = {
            "id": self.id,
            "display_name": self.display_name,
            "avatar_url": self.avatar_url,
            "role": self.role,
            "is_verified": self.is_verified,
            "plan": self.plan.to_dict() if self.plan else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if include_email:
            data["email"] = self.email
        return data
