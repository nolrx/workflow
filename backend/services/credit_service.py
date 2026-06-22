"""
Credit Service - Atomic credit operations

Provides thread-safe, atomic operations for credit balance management.
Uses database-level atomicity to prevent race conditions.
"""
import logging
from typing import Optional

from sqlalchemy import update

from backend.extensions import db
from backend.models.credit import CreditBalance, CreditTransaction, TeamCreditBalance

logger = logging.getLogger(__name__)


class InsufficientCreditsError(Exception):
    """Exception raised when user doesn't have enough credits"""
    pass


def deduct_credits(
    user_id: str,
    amount: int,
    operation: str,
    resource_type: str,
    resource_id: str,
    description: Optional[str] = None,
    team_id: Optional[str] = None
) -> bool:
    """
    Atomically deduct credits from user or team balance.

    Uses database-level atomic update to prevent race conditions.
    Only deducts if balance >= amount.

    Args:
        user_id: The user performing the operation
        amount: Number of credits to deduct (positive number)
        operation: Type of operation (e.g., "generate_outline", "generate_image")
        resource_type: Type of resource (e.g., "agent_run", "code_project")
        resource_id: ID of the associated resource
        description: Human-readable description
        team_id: If provided, deduct from team balance instead of user balance

    Returns:
        True if deduction was successful

    Raises:
        InsufficientCreditsError: If balance is insufficient
        Exception: For database errors
    """
    if amount <= 0:
        raise ValueError("Amount must be positive")

    try:
        if team_id:
            # Deduct from team balance
            result = db.session.execute(
                update(TeamCreditBalance)
                .where(TeamCreditBalance.team_id == team_id)
                .where(TeamCreditBalance.balance >= amount)
                .values(balance=TeamCreditBalance.balance - amount)
            )
            balance_source = f"team:{team_id}"
        else:
            # Deduct from user balance
            result = db.session.execute(
                update(CreditBalance)
                .where(CreditBalance.user_id == user_id)
                .where(CreditBalance.balance >= amount)
                .values(balance=CreditBalance.balance - amount)
            )
            balance_source = f"user:{user_id}"

        if result.rowcount == 0:
            # No rows updated means insufficient balance
            raise InsufficientCreditsError(f"Insufficient credits for {balance_source}")

        # Get the new balance for transaction record
        if team_id:
            balance_record = TeamCreditBalance.query.filter_by(team_id=team_id).first()
            balance_after = balance_record.balance if balance_record else 0
        else:
            balance_record = CreditBalance.query.filter_by(user_id=user_id).first()
            balance_after = balance_record.balance if balance_record else 0

        # Create transaction record
        transaction = CreditTransaction(
            user_id=user_id,
            team_id=team_id,
            amount=-amount,
            balance_after=balance_after,
            transaction_type="usage",
            operation=operation,
            resource_type=resource_type,
            resource_id=resource_id,
            description=description or f"{operation} for {resource_type}"
        )
        db.session.add(transaction)
        db.session.commit()

        logger.info(f"Deducted {amount} credits from {balance_source}, new balance: {balance_after}")
        return True

    except InsufficientCreditsError:
        db.session.rollback()
        raise
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to deduct credits: {e}")
        raise


def add_credits(
    user_id: str,
    amount: int,
    transaction_type: str,
    description: Optional[str] = None,
    team_id: Optional[str] = None,
    operation: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None
) -> int:
    """
    Add credits to user or team balance.

    Args:
        user_id: The user to add credits to
        amount: Number of credits to add (positive number)
        transaction_type: Type of transaction (e.g., "purchase", "bonus", "refund")
        description: Human-readable description
        team_id: If provided, add to team balance instead of user balance
        operation: Optional operation label for the audit record (e.g., "agent_run")
        resource_type: Optional resource type for the audit record
        resource_id: Optional resource id for the audit record

    Returns:
        New balance after addition

    Raises:
        Exception: For database errors
    """
    if amount <= 0:
        raise ValueError("Amount must be positive")

    try:
        if team_id:
            balance_record = TeamCreditBalance.query.filter_by(team_id=team_id).first()
            if not balance_record:
                balance_record = TeamCreditBalance(team_id=team_id, balance=0)
                db.session.add(balance_record)
            balance_record.balance += amount
            balance_source = f"team:{team_id}"
        else:
            balance_record = CreditBalance.query.filter_by(user_id=user_id).first()
            if not balance_record:
                balance_record = CreditBalance(user_id=user_id, balance=0)
                db.session.add(balance_record)
            balance_record.balance += amount
            balance_source = f"user:{user_id}"

        # Create transaction record
        transaction = CreditTransaction(
            user_id=user_id,
            team_id=team_id,
            amount=amount,
            balance_after=balance_record.balance,
            transaction_type=transaction_type,
            operation=operation,
            resource_type=resource_type,
            resource_id=resource_id,
            description=description or f"Credit {transaction_type}"
        )
        db.session.add(transaction)
        db.session.commit()

        logger.info(f"Added {amount} credits to {balance_source}, new balance: {balance_record.balance}")
        return balance_record.balance

    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to add credits: {e}")
        raise


def get_balance(user_id: str, team_id: Optional[str] = None) -> int:
    """
    Get current credit balance for user or team.

    Args:
        user_id: The user ID
        team_id: If provided, get team balance instead

    Returns:
        Current balance (0 if no record exists)
    """
    if team_id:
        record = TeamCreditBalance.query.filter_by(team_id=team_id).first()
    else:
        record = CreditBalance.query.filter_by(user_id=user_id).first()

    return record.balance if record else 0


def check_sufficient_credits(
    user_id: str,
    required_amount: int,
    team_id: Optional[str] = None
) -> bool:
    """
    Check if user/team has sufficient credits.

    Args:
        user_id: The user ID
        required_amount: Number of credits needed
        team_id: If provided, check team balance

    Returns:
        True if balance >= required_amount
    """
    return get_balance(user_id, team_id) >= required_amount


def charge(
    user_id: str,
    amount: int,
    operation: str,
    resource_type: str,
    resource_id: str,
    description: Optional[str] = None,
    team_id: Optional[str] = None
) -> bool:
    """
    Atomically deduct credits, returning False on insufficient balance instead of raising.

    Thin wrapper over :func:`deduct_credits` for callers in loops / background tasks
    that want to stop gracefully on insufficient funds rather than handle an exception.
    A zero/negative amount is treated as a no-op success (e.g., when an operation is
    priced free via the central pricing table).

    Returns:
        True if the deduction succeeded (or amount <= 0), False if balance was insufficient.

    Raises:
        Exception: For database errors (propagated from deduct_credits).
    """
    if amount <= 0:
        return True
    try:
        deduct_credits(
            user_id=user_id,
            amount=amount,
            operation=operation,
            resource_type=resource_type,
            resource_id=resource_id,
            description=description,
            team_id=team_id,
        )
        return True
    except InsufficientCreditsError:
        return False


def refund_credits(
    user_id: str,
    amount: int,
    operation: str,
    resource_type: str,
    resource_id: str,
    description: Optional[str] = None,
    team_id: Optional[str] = None
) -> int:
    """
    Refund previously charged credits back to a user or team balance.

    Implemented on top of :func:`add_credits` with ``transaction_type="refund"`` so the
    refund is captured as an audited transaction. A zero/negative amount is a no-op.

    Returns:
        The new balance after the refund (or current balance if amount <= 0).
    """
    if amount <= 0:
        return get_balance(user_id, team_id)
    return add_credits(
        user_id=user_id,
        amount=amount,
        transaction_type="refund",
        description=description or f"refund:{operation}",
        team_id=team_id,
        operation=operation,
        resource_type=resource_type,
        resource_id=resource_id,
    )
