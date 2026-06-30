"""
Generation-quality trend endpoint (the eval framework's read side, P0-B).

Read-only oversight over the stored ``CodeQualitySample`` rows: success rate,
mean rubric score, mean repair rounds, degraded rate and per-prompt-version /
per-model / per-day buckets. Owner-scoped by default (a caller sees only their
own runs' samples); an admin may pass ``?scope=all`` for a platform-wide view —
mirroring the App Space convention (see apps_routes / utils.auth.is_admin).
"""
from flask import Blueprint, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from backend.services.code.quality_metrics import summarize_quality
from backend.utils.auth import is_admin
from backend.utils.response import success_response

quality_bp = Blueprint("code_quality", __name__)


@quality_bp.route("/quality/trends", methods=["GET"])
@jwt_required()
def quality_trends():
    """Aggregated generation-quality trend, sliced by the query filters.

    Query params (all optional):
      - ``lane``        frontend | backend
      - ``kind``        online (default) | eval
      - ``window_days`` look-back window, default 30 (0 = all time)
      - ``project_id``  restrict to one project
      - ``team_id``     restrict to one team
      - ``scope=all``   admins only — platform-wide (otherwise own samples only)
    """
    user_id = get_jwt_identity()
    lane = (request.args.get("lane") or "").strip() or None
    kind = (request.args.get("kind") or "online").strip() or "online"
    project_id = (request.args.get("project_id") or "").strip() or None
    team_id = (request.args.get("team_id") or "").strip() or None
    admin_all = (request.args.get("scope") or "").strip().lower() == "all" and is_admin(user_id)

    try:
        window_days = max(0, int(request.args.get("window_days", 30)))
    except (TypeError, ValueError):
        window_days = 30

    data = summarize_quality(
        lane=lane,
        kind=kind,
        window_days=window_days,
        project_id=project_id,
        team_id=team_id,
        # Non-admins (or admins without scope=all) only ever see their own samples.
        user_id=None if admin_all else user_id,
    )
    data["scope"] = "all" if admin_all else "own"
    return success_response(data)
