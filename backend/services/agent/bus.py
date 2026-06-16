"""
In-memory event bus for live SSE delivery.

Events are persisted to ``agent_events`` (the source of truth, used for
reconnect/replay). The bus additionally fans freshly-persisted events out to
any currently-connected SSE streams so the UI updates in real time without
polling. A slow/stuck subscriber simply drops events and catches up via the DB
replay on its next sweep — the bus never blocks the worker thread.
"""
import logging
import queue
import threading

logger = logging.getLogger(__name__)


class AgentEventBus:
    """Fan-out of run events to live subscribers, keyed by run_id."""

    def __init__(self):
        self._subscribers: dict[str, set[queue.Queue]] = {}
        self._lock = threading.Lock()

    def subscribe(self, run_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self._lock:
            self._subscribers.setdefault(run_id, set()).add(q)
        return q

    def unsubscribe(self, run_id: str, q: queue.Queue) -> None:
        with self._lock:
            subs = self._subscribers.get(run_id)
            if subs:
                subs.discard(q)
                if not subs:
                    self._subscribers.pop(run_id, None)

    def publish(self, run_id: str, event: dict) -> None:
        with self._lock:
            subscribers = list(self._subscribers.get(run_id, ()))
        for q in subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                # Subscriber is behind. Drop the OLDEST event and keep this one, so
                # the newest events (notably the terminal run_completed) always win;
                # any dropped middle events are recovered via DB replay by sequence.
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(event)
                except queue.Full:
                    logger.debug("Event bus subscriber for run %s is full; dropping", run_id)


# Process-wide singleton (dev server is single-process, multi-threaded).
event_bus = AgentEventBus()
