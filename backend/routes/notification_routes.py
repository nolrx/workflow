"""
Notification routes (mounted at ``/api/notifications``).

The in-app notification feed for the current user. Unlike the older team blueprint
(which still returns bare ``jsonify`` shapes), this fresh surface uses the unified
``{success, data, message}`` envelope, so the frontend store reads ``res.data``.

    GET  /notifications               list my notices (?unread=1, ?limit/&offset)
    GET  /notifications/unread-count  {count} — cheap badge poll / fallback
    GET  /notifications/stream        live SSE push of new notices (real-time)
    POST /notifications/<id>/read     mark one read
    POST /notifications/read-all      mark all read
"""
import json
import queue

from flask import Blueprint, Response, current_app, request, stream_with_context
from flask_jwt_extended import get_jwt_identity, jwt_required

from backend.services import notification_service
from backend.services.notification_bus import notification_bus
from backend.utils.response import error_response, success_response

notification_bp = Blueprint("notifications", __name__)


@notification_bp.route("", methods=["GET"])
@jwt_required()
def list_notifications():
    user_id = get_jwt_identity()
    unread_only = request.args.get("unread") in ("1", "true", "True")
    try:
        limit = min(max(int(request.args.get("limit", 20)), 1), 100)
    except (TypeError, ValueError):
        limit = 20
    try:
        offset = max(int(request.args.get("offset", 0)), 0)
    except (TypeError, ValueError):
        offset = 0

    items, total, unread = notification_service.list_notifications(
        user_id, unread_only=unread_only, limit=limit, offset=offset
    )
    return success_response(
        {
            "notifications": [n.to_dict() for n in items],
            "total": total,
            "unread_count": unread,
            "limit": limit,
            "offset": offset,
        }
    )


@notification_bp.route("/unread-count", methods=["GET"])
@jwt_required()
def get_unread_count():
    user_id = get_jwt_identity()
    return success_response({"count": notification_service.unread_count(user_id)})


def _sse(event_name: str, payload: dict) -> str:
    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _notification_stream(app, user_id: str):
    """SSE generator: push freshly-committed notices live; keepalive otherwise.

    Holds one gthread worker thread for the connection's lifetime (mostly blocked
    on the queue, GIL released), so the frontend only opens it while the tab is
    visible and falls back to a slow badge poll when it can't. The DB stays the
    source of truth — a missed push is recovered by the client's next fetch.
    """
    q = notification_bus.subscribe(user_id)
    try:
        with app.app_context():
            unread = notification_service.unread_count(user_id)
        yield _sse("ready", {"unread_count": unread})
        while True:
            try:
                event = q.get(timeout=15)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            if event.get("kind") == "notification":
                yield _sse("notification", event["notification"])
    finally:
        notification_bus.unsubscribe(user_id, q)


@notification_bp.route("/stream", methods=["GET"])
@jwt_required()
def stream():
    """Live notification push as text/event-stream (consumed via fetch + reader)."""
    user_id = get_jwt_identity()
    app = current_app._get_current_object()
    response = Response(
        stream_with_context(_notification_stream(app, user_id)),
        mimetype="text/event-stream",
    )
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@notification_bp.route("/<notification_id>/read", methods=["POST"])
@jwt_required()
def mark_read(notification_id: str):
    user_id = get_jwt_identity()
    if not notification_service.mark_read(user_id, notification_id):
        return error_response("NOT_FOUND", "通知不存在", 404)
    return success_response({"unread_count": notification_service.unread_count(user_id)})


@notification_bp.route("/read-all", methods=["POST"])
@jwt_required()
def mark_all_read():
    user_id = get_jwt_identity()
    count = notification_service.mark_all_read(user_id)
    return success_response({"updated": count, "unread_count": 0})
