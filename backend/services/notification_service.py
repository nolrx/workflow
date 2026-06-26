"""
Notification service — the single place that creates and mutates in-app notices.

Creation is split from commit on purpose: ``create_notification`` only stages the
row (``add`` + ``flush``) so a caller can emit a notice as part of a larger
transaction (e.g. "invite member" persists the invitation and its notification in
one commit). The read/mutation helpers (mark read, count) own their own commit
since they are typically called stand-alone from the notification routes.
"""
import threading
from datetime import datetime

from sqlalchemy import event

from backend.extensions import db
from backend.models.notification import Notification
from backend.services.notification_bus import notification_bus

# Real-time delivery is published AFTER the producing transaction commits, so a
# rolled-back create never fires a phantom notice and a concurrent fetch always
# sees a consistent row. ``create_notification`` only stages the already-computed
# payload onto this thread-local list (the staging thread is the committing
# thread under both the request handlers and the agent worker threads); the
# ``after_commit`` hook drains and publishes it, ``after_rollback`` discards it.
_pending = threading.local()


def _stage_publish(user_id: str, payload: dict) -> None:
    items = getattr(_pending, "items", None)
    if items is None:
        items = []
        _pending.items = items
    items.append((user_id, payload))


@event.listens_for(db.session, "after_commit")
def _publish_pending(session) -> None:  # noqa: ANN001 — SQLAlchemy event signature
    items = getattr(_pending, "items", None)
    if not items:
        return
    _pending.items = []
    for user_id, payload in items:
        notification_bus.publish(user_id, {"kind": "notification", "notification": payload})


@event.listens_for(db.session, "after_rollback")
def _discard_pending(session) -> None:  # noqa: ANN001 — SQLAlchemy event signature
    if getattr(_pending, "items", None):
        _pending.items = []

# Severity levels (drive the frontend dot/icon colour). Keep in sync with the
# frontend ``NotificationLevel`` union.
LEVEL_INFO = "info"
LEVEL_SUCCESS = "success"
LEVEL_WARNING = "warning"
LEVEL_ERROR = "error"

# Well-known notification ``type`` keys. The feed is generic — ANY feature can emit
# a notice by calling ``create_notification`` with its own ``type`` and a matching
# frontend descriptor (see ``frontend/.../notificationTypes.ts``). These constants
# just document the types already wired so producers and the registry stay aligned.
#   - team_invite / team_invite_accepted / team_invite_rejected : team invitations
#   - code_deploy_succeeded                                     : a deploy run went live
#   - run_failed                                                : any agent run failed
# Convention for clickable notices: put a frontend route in ``data["link"]`` (e.g.
# "/apps/<project_id>"); the bell navigates there on click. Use ``ref_type``/``ref_id``
# to link back to the source object so it can be marked read when acted on.
TYPE_TEAM_INVITE = "team_invite"
TYPE_TEAM_INVITE_ACCEPTED = "team_invite_accepted"
TYPE_TEAM_INVITE_REJECTED = "team_invite_rejected"
TYPE_CODE_DEPLOY_SUCCEEDED = "code_deploy_succeeded"
TYPE_RUN_FAILED = "run_failed"


def create_notification(
    user_id: str,
    type: str,
    *,
    level: str = LEVEL_INFO,
    title: str | None = None,
    body: str | None = None,
    data: dict | None = None,
    ref_type: str | None = None,
    ref_id: str | None = None,
) -> Notification:
    """Stage a notification for ``user_id`` (caller owns the commit).

    Generic by design — ``type``/``level``/``data`` are free-form so any feature can
    emit a notice without touching this module; the frontend renders it from a
    type→descriptor registry (unknown types fall back to ``title``/``body``).
    """
    notification = Notification(
        user_id=user_id,
        type=type,
        level=level or LEVEL_INFO,
        title=title,
        body=body,
        data=data or {},
        ref_type=ref_type,
        ref_id=ref_id,
    )
    db.session.add(notification)
    db.session.flush()
    # Stage for real-time push once this transaction commits (payload captured now,
    # while attributes are populated — the after_commit hook must not emit SQL).
    _stage_publish(user_id, notification.to_dict())
    return notification


def list_notifications(
    user_id: str,
    *,
    unread_only: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[Notification], int, int]:
    """Return ``(items, total, unread_count)`` newest-first for one user."""
    base = Notification.query.filter_by(user_id=user_id)
    unread = base.filter_by(is_read=False).count()

    query = base
    if unread_only:
        query = query.filter_by(is_read=False)
    total = query.count()
    items = (
        query.order_by(Notification.created_at.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )
    return items, total, unread


def unread_count(user_id: str) -> int:
    return Notification.query.filter_by(user_id=user_id, is_read=False).count()


def mark_read(user_id: str, notification_id: str) -> bool:
    """Mark one notice read (scoped to the owner). Returns False if not found."""
    notification = Notification.query.filter_by(
        id=notification_id, user_id=user_id
    ).first()
    if not notification:
        return False
    if not notification.is_read:
        notification.is_read = True
        notification.read_at = datetime.utcnow()
        db.session.commit()
    return True


def mark_all_read(user_id: str) -> int:
    """Mark every unread notice for the user read. Returns the affected count."""
    now = datetime.utcnow()
    count = (
        Notification.query.filter_by(user_id=user_id, is_read=False)
        .update({"is_read": True, "read_at": now})
    )
    db.session.commit()
    return count


def mark_read_by_ref(
    user_id: str, ref_type: str, ref_id: str, *, commit: bool = True
) -> int:
    """Mark notices linked to a source object read (e.g. an acted-on invitation).

    Used inside the team accept/reject flow, hence the ``commit`` opt-out so it can
    ride the surrounding transaction.
    """
    now = datetime.utcnow()
    count = (
        Notification.query.filter_by(
            user_id=user_id, ref_type=ref_type, ref_id=ref_id, is_read=False
        ).update({"is_read": True, "read_at": now})
    )
    if commit:
        db.session.commit()
    return count
