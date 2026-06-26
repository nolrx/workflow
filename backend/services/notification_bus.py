"""
In-memory pub/sub for live notification delivery, keyed by ``user_id``.

Mirrors the agent event bus (``services/agent/bus.py``) but fans a freshly-committed
notification out to that user's currently-connected SSE stream(s) — so the bell
updates in real time without polling. The DB row stays the source of truth: a
dropped/slow subscriber simply misses the push and catches up on its next fetch
(badge fallback poll / on-open list fetch). The bus never blocks the producer.

Single-process by design (one gunicorn worker — see gunicorn.conf.py), same as the
agent bus, so cross-thread delivery within the process is all that's needed.
"""
import logging
import queue
import threading

logger = logging.getLogger(__name__)


class NotificationBus:
    """Fan-out of notifications to live subscribers, keyed by user_id."""

    def __init__(self):
        self._subscribers: dict[str, set[queue.Queue]] = {}
        self._lock = threading.Lock()

    def subscribe(self, user_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._subscribers.setdefault(user_id, set()).add(q)
        return q

    def unsubscribe(self, user_id: str, q: queue.Queue) -> None:
        with self._lock:
            subs = self._subscribers.get(user_id)
            if subs:
                subs.discard(q)
                if not subs:
                    self._subscribers.pop(user_id, None)

    def publish(self, user_id: str, event: dict) -> None:
        with self._lock:
            subscribers = list(self._subscribers.get(user_id, ()))
        for q in subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                # Subscriber is behind: drop the oldest and keep the newest. Missed
                # items are recovered by the client's fallback fetch — never block.
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(event)
                except queue.Full:
                    logger.debug("notification bus subscriber for %s full; dropping", user_id)


# Process-wide singleton (single gthread worker — see gunicorn.conf.py).
notification_bus = NotificationBus()
