"""
Shared authorization helpers.

``admin_required`` guards admin-only endpoints. It composes with
``@jwt_required()`` (which must run first to populate the JWT identity) and
returns the unified 403 error shape when the caller is not an admin.
"""
from functools import wraps

from flask_jwt_extended import get_jwt_identity, jwt_required

from backend.models.user import User
from backend.utils.response import error_response


def current_user() -> User | None:
    """Return the User for the current JWT identity, or None."""
    user_id = get_jwt_identity()
    if not user_id:
        return None
    return User.query.get(user_id)


def admin_required(fn):
    """Require a valid JWT belonging to a user with role == "admin".

    Wraps the view in ``@jwt_required()`` so callers only need this one
    decorator. Returns 403 FORBIDDEN for authenticated non-admins.
    """

    @wraps(fn)
    @jwt_required()
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            return error_response("NOT_FOUND", "用户不存在", 404)
        if user.role != "admin":
            return error_response("FORBIDDEN", "需要管理员权限", 403)
        return fn(*args, **kwargs)

    return wrapper
