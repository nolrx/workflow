"""
Credit system routes
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from backend.models.credit import CreditBalance, CreditTransaction, TeamCreditBalance
from backend.utils.response import error_response

credit_bp = Blueprint("credits", __name__)


def _is_team_member(team_id: str, user_id: str) -> bool:
    """Return True if the user belongs to the team."""
    from backend.models.team import TeamMember
    return (
        TeamMember.query.filter_by(team_id=team_id, user_id=user_id).first()
        is not None
    )


# Empty balance shape returned when no balance row exists yet (kept raw — the
# frontend reads /credits/balance as a bare object, not a success envelope).
_EMPTY_BALANCE = {"balance": 0, "monthly_allocation": 0, "monthly_used": 0}


@credit_bp.route("/balance", methods=["GET"])
@jwt_required()
def get_balance():
    """Get the current user's — or, with ?team_id=, a team's — credit balance."""
    user_id = get_jwt_identity()
    team_id = request.args.get("team_id")

    if team_id:
        if not _is_team_member(team_id, user_id):
            return error_response("FORBIDDEN", "Access denied", 403)
        balance = TeamCreditBalance.query.get(team_id)
    else:
        balance = CreditBalance.query.get(user_id)

    if not balance:
        return jsonify(_EMPTY_BALANCE)
    return jsonify(balance.to_dict())


@credit_bp.route("/transactions", methods=["GET"])
@jwt_required()
def list_transactions():
    """List credit transactions for the current user, or a team with ?team_id=."""
    user_id = get_jwt_identity()
    team_id = request.args.get("team_id")

    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)

    if team_id:
        if not _is_team_member(team_id, user_id):
            return error_response("FORBIDDEN", "Access denied", 403)
        query = CreditTransaction.query.filter_by(team_id=team_id)
    else:
        query = CreditTransaction.query.filter_by(user_id=user_id)
    query = query.order_by(CreditTransaction.created_at.desc())

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    return jsonify({
        "transactions": [t.to_dict() for t in pagination.items],
        "total": pagination.total,
        "page": page,
        "per_page": per_page,
        "has_more": pagination.has_next,
    })


@credit_bp.route("/usage", methods=["GET"])
@jwt_required()
def get_usage_stats():
    """Get credit usage statistics for the current user, or a team with ?team_id=."""
    user_id = get_jwt_identity()
    team_id = request.args.get("team_id")

    if team_id:
        if not _is_team_member(team_id, user_id):
            return error_response("FORBIDDEN", "Access denied", 403)
        balance = TeamCreditBalance.query.get(team_id)
    else:
        balance = CreditBalance.query.get(user_id)

    if not balance:
        return jsonify({
            "total_credits": 0,
            "used_credits": 0,
            "remaining_credits": 0,
            "usage_percentage": 0,
        })

    total = balance.monthly_allocation
    used = balance.monthly_used
    remaining = balance.balance

    return jsonify({
        "total_credits": total,
        "used_credits": used,
        "remaining_credits": remaining,
        "usage_percentage": round((used / total * 100) if total > 0 else 0, 1),
    })
