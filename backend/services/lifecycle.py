"""
Process lifecycle — graceful-drain state for single-machine redeploys.

This deployment runs ONE gunicorn worker (the Agent runtime's ThreadPoolExecutor
and the SSE event bus are process-local singletons), so two app instances can't
safely run side by side — a redeploy recreates the single container. To make that
graceful we DRAIN first: stop accepting NEW background work, let the deploy swap
the container, and let the next process resume any in-flight run from its
persisted phase (``reconcile_orphaned_runs``) while SSE clients auto-reconnect.

This module holds the process-wide drain flag. It lives only in memory, so a
freshly started process is never draining — exactly what we want after a swap.
Read-only / in-flight endpoints stay available while draining; only the endpoints
that START new work consult ``drain_guard``.
"""
import threading

from backend.utils.response import error_response

_draining = threading.Event()


def begin_drain() -> None:
    """Enter drain mode — new background work is refused until the process exits."""
    _draining.set()


def end_drain() -> None:
    """Leave drain mode (mainly for tests / a cancelled deploy)."""
    _draining.clear()


def is_draining() -> bool:
    return _draining.is_set()


def drain_guard():
    """Return a 503 response when draining, else ``None``.

    Call at the top of any endpoint that STARTS new background work (creating /
    resuming / retrying agent runs, starting the full-stack pipeline or a deploy).
    Read-only and in-flight endpoints (run snapshot, SSE stream, cancel) must NOT
    use this — users keep watching and reconnecting to running work during a deploy.
    """
    if is_draining():
        return error_response("DRAINING", "平台正在发布，请稍后重试", 503)
    return None
