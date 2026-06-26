"""
Gunicorn config — single gthread worker + graceful drain on shutdown.

A SINGLE worker with multiple threads is intentional: the Agent Swarm runtime,
SSE event bus and recorder are process-level singletons, so cross-request SSE
replay only works inside one process. gthread keeps long-lived SSE streams alive
without blocking other requests.

On SIGTERM (``docker stop`` / ``compose up`` recreate) we flip the process into
DRAIN *before* gunicorn's graceful shutdown runs, so no new background work is
accepted during the shutdown window — a safety net for an un-orchestrated stop.
The deploy script normally drains explicitly first (scripts/deploy-backend.sh);
either way, in-flight runs are resumed by the next process from their persisted
phase (reconcile_orphaned_runs). Used via ``gunicorn ... -c backend/gunicorn.conf.py``.
"""
import os
import signal

bind = "0.0.0.0:5001"
workers = 1
# Each long-lived SSE stream (an agent-run watch + the per-user notification
# stream) holds one thread for its lifetime — mostly blocked on a queue with the
# GIL released — so the headroom above the original 8 leaves room for concurrent
# live streams plus the regular request load. Env-tunable; the frontend only opens
# a notification stream while the tab is visible, keeping the steady-state low.
threads = int(os.getenv("GUNICORN_THREADS", "16"))
worker_class = "gthread"
timeout = 600
graceful_timeout = 30
accesslog = "-"
errorlog = "-"
# Docker Desktop on Windows can leave gunicorn's default worker heartbeat tmp
# file on a host-shared filesystem and raise FileNotFoundError during notify().
# /dev/shm is an in-container tmpfs, which avoids the issue and is safe on
# Linux hosts as well.
worker_tmp_dir = "/dev/shm"


def post_worker_init(worker):
    """Chain a drain-on-SIGTERM handler in front of gunicorn's own.

    Runs inside the worker (where the in-process drain flag lives). We preserve
    gunicorn's existing SIGTERM handler so its graceful shutdown still happens —
    we just set the drain flag first.
    """
    from backend.services.lifecycle import begin_drain

    previous = signal.getsignal(signal.SIGTERM)

    def _drain_then_shutdown(signum, frame):
        try:
            begin_drain()
        finally:
            if callable(previous):
                previous(signum, frame)

    signal.signal(signal.SIGTERM, _drain_then_shutdown)
