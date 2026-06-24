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
import signal

bind = "0.0.0.0:5001"
workers = 1
threads = 8
worker_class = "gthread"
timeout = 600
graceful_timeout = 30
accesslog = "-"
errorlog = "-"


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
