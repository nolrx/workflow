"""
Credit system models
"""
import uuid
from datetime import datetime
from backend.extensions import db


class CreditBalance(db.Model):
    """User credit balance model."""
    __tablename__ = "credit_balances"

    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), primary_key=True)
    balance = db.Column(db.Integer, default=0)
    monthly_allocation = db.Column(db.Integer, default=0)
    monthly_used = db.Column(db.Integer, default=0)
    allocation_reset_at = db.Column(db.DateTime)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = db.relationship("User", back_populates="credit_balance")

    def to_dict(self) -> dict:
        return {
            "balance": self.balance,
            "monthly_allocation": self.monthly_allocation,
            "monthly_used": self.monthly_used,
            "allocation_reset_at": self.allocation_reset_at.isoformat() if self.allocation_reset_at else None,
        }


class TeamCreditBalance(db.Model):
    """Team credit balance model."""
    __tablename__ = "team_credit_balances"

    team_id = db.Column(db.String(36), db.ForeignKey("teams.id"), primary_key=True)
    balance = db.Column(db.Integer, default=0)
    monthly_allocation = db.Column(db.Integer, default=0)
    monthly_used = db.Column(db.Integer, default=0)
    allocation_reset_at = db.Column(db.DateTime)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    team = db.relationship("Team", back_populates="credit_balance")

    def to_dict(self) -> dict:
        return {
            "balance": self.balance,
            "monthly_allocation": self.monthly_allocation,
            "monthly_used": self.monthly_used,
            "allocation_reset_at": self.allocation_reset_at.isoformat() if self.allocation_reset_at else None,
        }


class CreditTransaction(db.Model):
    """Credit transaction record."""
    __tablename__ = "credit_transactions"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    team_id = db.Column(db.String(36), db.ForeignKey("teams.id"), nullable=True, index=True)

    # Transaction details
    amount = db.Column(db.Integer, nullable=False)  # positive = add, negative = deduct
    balance_after = db.Column(db.Integer, nullable=False)
    transaction_type = db.Column(db.String(50))  # allocation, usage, purchase, refund, bonus

    # Usage details
    operation = db.Column(db.String(50))  # generate_outline, generate_image, export
    resource_type = db.Column(db.String(50))  # agent_run, code_project
    resource_id = db.Column(db.String(36))

    # Metadata
    description = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    # Relationships
    user = db.relationship("User", backref="credit_transactions")
    team = db.relationship("Team", backref="credit_transactions")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "team_id": self.team_id,
            "amount": self.amount,
            "balance_after": self.balance_after,
            "transaction_type": self.transaction_type,
            "operation": self.operation,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "description": self.description,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
