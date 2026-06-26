"""
Team management routes
"""
import re
from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required
from sqlalchemy import func

from backend.extensions import db
from backend.models.credit import TeamCreditBalance
from backend.models.team import Team, TeamInvitation, TeamMember
from backend.models.user import User
from backend.services import notification_service

team_bp = Blueprint("teams", __name__)


def _display_name(user: User | None) -> str | None:
    if not user:
        return None
    return user.display_name or user.email


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

    email = (data.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "Email is required"}), 400

    role = data.get("role", "member")
    if role not in ("admin", "member", "viewer"):
        role = "member"

    # If the email belongs to a platform user, we'll notify them in-app and must
    # guard against re-inviting someone who already joined.
    invitee = User.query.filter(func.lower(User.email) == email).first()
    if invitee:
        already = TeamMember.query.filter_by(team_id=team_id, user_id=invitee.id).first()
        if already:
            return jsonify({"error": "User is already a team member"}), 409

    # Avoid duplicate PENDING invitations — accepted/rejected/expired don't block a re-invite.
    existing = TeamInvitation.query.filter_by(
        team_id=team_id,
        email=email,
        accepted_at=None,
        rejected_at=None,
    ).first()
    if existing and not existing.is_expired:
        return jsonify({"error": "Invitation already sent"}), 409

    # Create invitation
    invitation = TeamInvitation(
        team_id=team_id,
        email=email,
        role=role,
        invited_by=user_id,
    )
    db.session.add(invitation)
    db.session.flush()

    team = Team.query.get(team_id)
    inviter_name = _display_name(User.query.get(user_id))
    # Surface an in-app notice so a registered invitee can act on it (accept/reject).
    if invitee:
        notification_service.create_notification(
            invitee.id,
            notification_service.TYPE_TEAM_INVITE,
            level=notification_service.LEVEL_INFO,
            title=team.name if team else None,
            body=f"{inviter_name} 邀请你加入团队「{team.name if team else ''}」",
            data={
                "team_id": team_id,
                "team_name": team.name if team else None,
                "role": role,
                "token": invitation.token,
                "invitation_id": invitation.id,
                "inviter_name": inviter_name,
            },
            ref_type="team_invitation",
            ref_id=invitation.id,
        )
    db.session.commit()

    return jsonify({
        "message": "Invitation sent successfully",
        "invitation": invitation.to_dict(),
        "notified": bool(invitee),
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

    if invitation.is_rejected:
        return jsonify({"error": "Invitation was already rejected"}), 409

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
    # Clear the invitee's own pending notice and let the inviter know.
    notification_service.mark_read_by_ref(user_id, "team_invitation", invitation.id, commit=False)
    if invitation.invited_by and invitation.invited_by != user_id:
        notification_service.create_notification(
            invitation.invited_by,
            notification_service.TYPE_TEAM_INVITE_ACCEPTED,
            level=notification_service.LEVEL_SUCCESS,
            title=invitation.team.name if invitation.team else None,
            body=f"{_display_name(user)} 已加入团队「{invitation.team.name if invitation.team else ''}」",
            data={
                "team_id": invitation.team_id,
                "team_name": invitation.team.name if invitation.team else None,
                "user_name": _display_name(user),
            },
            ref_type="team_invitation",
            ref_id=invitation.id,
        )
    db.session.commit()

    return jsonify({
        "message": "Invitation accepted",
        "team": invitation.team.to_dict(),
    })


@team_bp.route("/invitations/<token>/reject", methods=["POST"])
@jwt_required()
def reject_invitation(token: str):
    """Reject (decline) a team invitation addressed to the current user."""
    user_id = get_jwt_identity()
    user = User.query.get(user_id)

    invitation = TeamInvitation.query.filter_by(token=token).first()
    if not invitation:
        return jsonify({"error": "Invitation not found"}), 404

    if invitation.email.lower() != user.email.lower():
        return jsonify({"error": "Invitation is for a different email"}), 403

    if invitation.is_accepted:
        return jsonify({"error": "Invitation already accepted"}), 409

    # Idempotent: re-rejecting is a no-op success.
    if not invitation.is_rejected:
        invitation.rejected_at = datetime.utcnow()
        notification_service.mark_read_by_ref(
            user_id, "team_invitation", invitation.id, commit=False
        )
        if invitation.invited_by and invitation.invited_by != user_id:
            notification_service.create_notification(
                invitation.invited_by,
                notification_service.TYPE_TEAM_INVITE_REJECTED,
                level=notification_service.LEVEL_WARNING,
                title=invitation.team.name if invitation.team else None,
                body=f"{_display_name(user)} 拒绝了加入团队「{invitation.team.name if invitation.team else ''}」的邀请",
                data={
                    "team_id": invitation.team_id,
                    "team_name": invitation.team.name if invitation.team else None,
                    "user_name": _display_name(user),
                },
                ref_type="team_invitation",
                ref_id=invitation.id,
            )
        db.session.commit()

    return jsonify({"message": "Invitation rejected"})


@team_bp.route("/invitations/pending", methods=["GET"])
@jwt_required()
def list_pending_invitations():
    """List actionable invitations addressed to the current user's email."""
    user = User.query.get(get_jwt_identity())
    if not user:
        return jsonify({"error": "User not found"}), 404

    invitations = (
        TeamInvitation.query.filter(
            func.lower(TeamInvitation.email) == user.email.lower(),
            TeamInvitation.accepted_at.is_(None),
            TeamInvitation.rejected_at.is_(None),
        )
        .order_by(TeamInvitation.created_at.desc())
        .all()
    )
    pending = [inv.to_dict() for inv in invitations if not inv.is_expired]
    return jsonify({"invitations": pending})
