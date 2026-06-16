"""
Team management routes
"""
import re
from datetime import datetime
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from backend.extensions import db
from backend.models.user import User
from backend.models.team import Team, TeamMember, TeamInvitation
from backend.models.credit import TeamCreditBalance

team_bp = Blueprint("teams", __name__)


def generate_slug(name: str) -> str:
    """Generate URL-friendly slug from team name."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name.lower()).strip("-")
    # Ensure uniqueness
    base_slug = slug
    counter = 1
    while Team.query.filter_by(slug=slug).first():
        slug = f"{base_slug}-{counter}"
        counter += 1
    return slug


@team_bp.route("", methods=["POST"])
@jwt_required()
def create_team():
    """Create a new team."""
    user_id = get_jwt_identity()
    data = request.get_json()

    if not data.get("name"):
        return jsonify({"error": "Team name is required"}), 400

    # Create team
    team = Team(
        name=data["name"],
        slug=generate_slug(data["name"]),
        owner_id=user_id,
    )
    db.session.add(team)
    db.session.flush()  # Get team ID

    # Add owner as team member
    member = TeamMember(
        team_id=team.id,
        user_id=user_id,
        role="owner",
    )
    db.session.add(member)

    # Create team credit balance
    credit_balance = TeamCreditBalance(
        team_id=team.id,
        balance=0,
        monthly_allocation=0,
    )
    db.session.add(credit_balance)

    db.session.commit()

    return jsonify({
        "message": "Team created successfully",
        "team": team.to_dict(),
    }), 201


@team_bp.route("", methods=["GET"])
@jwt_required()
def list_teams():
    """List teams for current user."""
    user_id = get_jwt_identity()

    memberships = TeamMember.query.filter_by(user_id=user_id).all()
    teams = [m.team.to_dict() for m in memberships if m.team]

    return jsonify({"teams": teams})


@team_bp.route("/<team_id>", methods=["GET"])
@jwt_required()
def get_team(team_id: str):
    """Get team details."""
    user_id = get_jwt_identity()

    team = Team.query.get(team_id)
    if not team:
        return jsonify({"error": "Team not found"}), 404

    # Check membership
    member = TeamMember.query.filter_by(team_id=team_id, user_id=user_id).first()
    if not member:
        return jsonify({"error": "Not a member of this team"}), 403

    return jsonify({"team": team.to_dict()})


@team_bp.route("/<team_id>/members", methods=["GET"])
@jwt_required()
def list_members(team_id: str):
    """List team members."""
    user_id = get_jwt_identity()

    # Check membership
    member = TeamMember.query.filter_by(team_id=team_id, user_id=user_id).first()
    if not member:
        return jsonify({"error": "Not a member of this team"}), 403

    members = TeamMember.query.filter_by(team_id=team_id).all()

    return jsonify({"members": [m.to_dict() for m in members]})


@team_bp.route("/<team_id>/invitations", methods=["POST"])
@jwt_required()
def invite_member(team_id: str):
    """Invite a user to the team."""
    user_id = get_jwt_identity()
    data = request.get_json()

    # Check membership and permissions
    member = TeamMember.query.filter_by(team_id=team_id, user_id=user_id).first()
    if not member or member.role not in ["owner", "admin"]:
        return jsonify({"error": "Permission denied"}), 403

    if not data.get("email"):
        return jsonify({"error": "Email is required"}), 400

    # Check if already invited
    existing = TeamInvitation.query.filter_by(
        team_id=team_id,
        email=data["email"],
        accepted_at=None,
    ).first()
    if existing and not existing.is_expired:
        return jsonify({"error": "Invitation already sent"}), 409

    # Create invitation
    invitation = TeamInvitation(
        team_id=team_id,
        email=data["email"],
        role=data.get("role", "member"),
        invited_by=user_id,
    )
    db.session.add(invitation)
    db.session.commit()

    return jsonify({
        "message": "Invitation sent successfully",
        "invitation": invitation.to_dict(),
    }), 201


@team_bp.route("/invitations/<token>/accept", methods=["POST"])
@jwt_required()
def accept_invitation(token: str):
    """Accept a team invitation."""
    user_id = get_jwt_identity()
    user = User.query.get(user_id)

    invitation = TeamInvitation.query.filter_by(token=token).first()
    if not invitation:
        return jsonify({"error": "Invitation not found"}), 404

    if invitation.is_expired:
        return jsonify({"error": "Invitation has expired"}), 410

    if invitation.is_accepted:
        return jsonify({"error": "Invitation already accepted"}), 409

    if invitation.email.lower() != user.email.lower():
        return jsonify({"error": "Invitation is for a different email"}), 403

    # Check if already a member
    existing = TeamMember.query.filter_by(
        team_id=invitation.team_id,
        user_id=user_id,
    ).first()
    if existing:
        return jsonify({"error": "Already a member of this team"}), 409

    # Add as team member
    member = TeamMember(
        team_id=invitation.team_id,
        user_id=user_id,
        role=invitation.role,
    )
    db.session.add(member)

    # Mark invitation as accepted
    invitation.accepted_at = datetime.utcnow()
    db.session.commit()

    return jsonify({
        "message": "Invitation accepted",
        "team": invitation.team.to_dict(),
    })
