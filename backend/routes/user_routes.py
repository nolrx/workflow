"""
User management routes
"""
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from backend.extensions import db
from backend.models.user import User

user_bp = Blueprint("users", __name__)


@user_bp.route("/profile", methods=["GET"])
@jwt_required()
def get_profile():
    """Get current user's profile."""
    user_id = get_jwt_identity()
    user = User.query.get(user_id)

    if not user:
        return jsonify({"error": "User not found"}), 404

    return jsonify({"user": user.to_dict()})


@user_bp.route("/profile", methods=["PUT"])
@jwt_required()
def update_profile():
    """Update current user's profile."""
    user_id = get_jwt_identity()
    user = User.query.get(user_id)

    if not user:
        return jsonify({"error": "User not found"}), 404

    data = request.get_json()

    # Update allowed fields
    if "display_name" in data:
        user.display_name = data["display_name"]
    if "avatar_url" in data:
        user.avatar_url = data["avatar_url"]

    db.session.commit()

    return jsonify({
        "message": "Profile updated successfully",
        "user": user.to_dict(),
    })


@user_bp.route("/password", methods=["PUT"])
@jwt_required()
def change_password():
    """Change current user's password."""
    user_id = get_jwt_identity()
    user = User.query.get(user_id)

    if not user:
        return jsonify({"error": "User not found"}), 404

    data = request.get_json()

    if not data.get("current_password") or not data.get("new_password"):
        return jsonify({"error": "Current password and new password are required"}), 400

    if not user.check_password(data["current_password"]):
        return jsonify({"error": "Current password is incorrect"}), 401

    user.set_password(data["new_password"])
    db.session.commit()

    return jsonify({"message": "Password changed successfully"})
