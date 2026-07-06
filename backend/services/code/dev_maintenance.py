"""
Dev Mode background maintenance — idle reaping + crash self-heal.

A long-running dev container per project is a real resource liability, so it must
be reaped when idle and healed when it dies. Two entry points:

  * ``reconcile_session(session, dev)`` — cheap, read-time status sync (used by the
    dev routes on GET): mirror the container's actual running/restart state onto the
    session row; mark a vanished container's session STOPPED. No commit (caller owns it).
  * ``reap_and_heal_once(app)`` / ``start_maintenance_daemon(app)`` — the periodic
    maintenance loop (started ONLY on the real server boot, like reconcile_orphaned_runs):
      - IDLE reap: a session whose ``last_active_at`` is older than
        ``DEV_MODE_IDLE_REAP_SECONDS`` has its container torn down (frees CPU/mem +
        the injected credentials that live in the container env).
      - CRASH self-heal: an ACTIVE session whose container has VANISHED (removed, not
        just kernel-restarted) is restarted from the latest source, bounded by
        ``DEV_MODE_MAX_HEAL`` attempts, then failed if it keeps dying.

All fail-soft: maintenance never raises into the caller / never blocks startup.
Comments in English to match the Code/core convention.
"""
import logging
import os
import threading
import time
from datetime import datetime

from backend.extensions import db
from backend.models.code.fullstack import CodeDevSession, DevSessionStatus
from backend.services.code.dev_backend_service import get_dev_backend_service
from backend.services.code.dev_service import DEV_IDLE_REAP_SECONDS, get_dev_service

logger = logging.getLogger(__name__)


def _dev_for(session: CodeDevSession):
    """The dev container service for a session's lane (frontend Vite / backend hot-reload)."""
    return get_dev_backend_service() if getattr(session, "lane", "frontend") == "backend" else get_dev_service()

DEV_MAINTENANCE_INTERVAL = int(os.getenv("DEV_MODE_MAINTENANCE_INTERVAL", "300"))
DEV_MAX_HEAL = int(os.getenv("DEV_MODE_MAX_HEAL", "3"))
# A container present-but-not-running with this many kernel (--restart) restarts is
# treated as a CRASH-LOOP and re-created from current code (a stale baked entrypoint
# from before a code fix would otherwise loop forever — `docker rm -f` + fresh run
# lets the fix take over). Also covers a genuinely broken build that keeps dying.
DEV_CRASHLOOP_RESTARTS = int(os.getenv("DEV_MODE_CRASHLOOP_RESTARTS", "3"))

_daemon_started = False
_daemon_lock = threading.Lock()


def reconcile_session(session: CodeDevSession, dev=None) -> bool:
    """Sync a session row with its container's real state. Returns True if changed.

    Read-time reconcile (does NOT commit — the caller owns the transaction): the
    dev routes call this on GET so a stale session reflects a vanished/restarted
    container without a background pass."""
    if session.status in DevSessionStatus.TERMINAL:
        return False
    dev = dev or _dev_for(session)
    try:
        st = dev.container_status(session.project_id)
    except Exception:  # noqa: BLE001
        return False
    changed = False
    if not st.get("present"):
        if session.status in DevSessionStatus.ACTIVE:
            session.status = DevSessionStatus.STOPPED
            changed = True
    else:
        rc = st.get("restart_count", 0)
        if rc != session.restart_count:
            session.restart_count = rc
            changed = True
        health = "healthy" if st.get("running") else "unhealthy"
        if health != session.health:
            session.health = health
            changed = True
        if st.get("running") and session.status == DevSessionStatus.STARTING:
            session.status = DevSessionStatus.RUNNING
            changed = True
    return changed


def _is_idle(session: CodeDevSession) -> bool:
    if DEV_IDLE_REAP_SECONDS <= 0:
        return False
    ref = session.last_active_at or session.updated_at or session.created_at
    if not ref:
        return False
    return (datetime.utcnow() - ref).total_seconds() > DEV_IDLE_REAP_SECONDS


def _resolve_source_for(session: CodeDevSession) -> dict:
    """The seed source for healing, per lane (frontend Vite / backend project)."""
    if getattr(session, "lane", "frontend") == "backend":
        from backend.services.agent.workflows.code_dev_backend_turn_workflow import (
            _resolve_backend_source,
        )

        return _resolve_backend_source(session.project_id)
    from backend.services.agent.workflows.code_dev_turn_workflow import _resolve_source

    return _resolve_source(session.project_id)


def _heal_session(session: CodeDevSession, dev) -> bool:
    """Restart a vanished dev container from the latest source (bounded). Returns healed."""
    detail = session.get_detail()
    heal_count = int(detail.get("heal_count", 0))
    if heal_count >= DEV_MAX_HEAL:
        session.status = DevSessionStatus.FAILED
        session.error_message = f"dev 容器多次异常终止(已尝试自愈 {heal_count} 次),已停止自动恢复"
        return False
    try:
        ok, err, info = dev.start_container(session.project_id, _resolve_source_for(session))
    except Exception as exc:  # noqa: BLE001
        ok, err, info = False, str(exc), {}
    if ok:
        session.container_name = info.get("container_name")
        session.internal_port = info.get("internal_port")
        session.workdir = info.get("workdir")
        # Backend start_container returns no preview_path (it's /preview/<pid>/api,
        # already on the row) — only overwrite when the lane provides one.
        if info.get("preview_path"):
            session.preview_path = info.get("preview_path")
        session.status = DevSessionStatus.RUNNING
        session.health = "healthy"
        detail["heal_count"] = heal_count + 1
        session.set_detail(detail)
        return True
    session.status = DevSessionStatus.FAILED
    session.error_message = f"dev 容器自愈失败：{err}"
    return False


def _snapshot_session(session: CodeDevSession, dev) -> bool:
    """Persist the session's container source as a durable artifact (best-effort).

    A universal safety net (any web product, not one project): the dev container fs
    is ephemeral, so before it can be torn down / lost, snapshot the work so a
    re-entry restores it. Per-turn persistence already covers active development;
    this covers reaping + boot-backfill of sessions built before that was in place."""
    try:
        from backend.models.agent.run import AgentRun

        lane = getattr(session, "lane", "frontend")
        if lane == "backend":
            from backend.services.agent.workflows.code_dev_backend_turn_workflow import (
                persist_backend_snapshot_standalone as _persist,
            )
            workflow = "code_dev_backend_turn"
        else:
            from backend.services.agent.workflows.code_dev_turn_workflow import (
                persist_snapshot_standalone as _persist,
            )
            workflow = "code_dev_turn"

        files = dev.collect_source(session.project_id)
        if not files:
            return False
        run = (
            AgentRun.query.filter_by(resource_id=session.project_id, workflow=workflow)
            .order_by(AgentRun.created_at.desc())
            .first()
        )
        if not run:
            return False
        return _persist(run.id, session.project_id, files)
    except Exception:  # noqa: BLE001
        return False


def backfill_snapshots(app) -> int:
    """Snapshot every RUNNING dev session ONCE (best-effort) so work built BEFORE the
    per-turn persistence was in place isn't lost on the next stop/reap. Called once at
    daemon start — a universal backfill, not a per-session rescue."""
    persisted = 0
    with app.app_context():
        try:
            sessions = CodeDevSession.query.filter(
                CodeDevSession.status.in_(list(DevSessionStatus.ACTIVE))
            ).all()
        except Exception:  # noqa: BLE001
            return 0
        for s in sessions:
            try:
                dev = _dev_for(s)
                if dev.container_status(s.project_id).get("running") and _snapshot_session(s, dev):
                    persisted += 1
            except Exception:  # noqa: BLE001
                pass
    if persisted:
        logger.info("dev snapshot backfill: persisted %d running session(s)", persisted)
    return persisted


def reap_and_heal_once(app) -> dict:
    """One maintenance pass: reap idle sessions, self-heal crashed ones."""
    reaped = healed = synced = 0
    with app.app_context():
        sessions = (
            CodeDevSession.query.filter(CodeDevSession.status.in_(list(DevSessionStatus.ACTIVE)))
            .all()
        )
        for s in sessions:
            try:
                dev = _dev_for(s)  # per-session: frontend Vite vs backend hot-reload
                if _is_idle(s):
                    _snapshot_session(s, dev)  # snapshot the work BEFORE rm -f
                    dev.stop_container(s.project_id)
                    s.status = DevSessionStatus.STOPPED
                    s.stopped_at = datetime.utcnow()
                    reaped += 1
                    continue
                st = dev.container_status(s.project_id)
                # Crash-loop: a --restart container cycles through a brief "running"
                # moment, so we can't gate on !running (it's racy). The robust signal
                # is a high restart count AND the dev server not actually serving.
                crashloop = (
                    st.get("present")
                    and st.get("restart_count", 0) >= DEV_CRASHLOOP_RESTARTS
                    and not dev.health_check(s.project_id)
                )
                if not st.get("present") or crashloop:
                    # Vanished OR crash-looping → re-create from current code
                    # (start_container removes the old/broken container first, so a
                    # stale baked entrypoint from before a fix is replaced).
                    if _heal_session(s, dev):
                        healed += 1
                    else:
                        # Gave up (past the heal cap) → stop the container so it does
                        # NOT keep kernel-restarting forever after the session failed.
                        dev.stop_container(s.project_id)
                else:
                    if reconcile_session(s, dev):
                        synced += 1
            except Exception:  # noqa: BLE001 — one bad row must not abort the pass
                logger.warning("dev maintenance failed for session %s", s.id, exc_info=True)
                db.session.rollback()
        try:
            db.session.commit()
        except Exception:  # noqa: BLE001
            db.session.rollback()
    if reaped or healed:
        logger.info("dev maintenance: reaped=%d healed=%d synced=%d", reaped, healed, synced)
    return {"reaped": reaped, "healed": healed, "synced": synced}


def start_maintenance_daemon(app) -> None:
    """Start the periodic dev-maintenance loop (idempotent; server boot only)."""
    global _daemon_started
    with _daemon_lock:
        if _daemon_started:
            return
        _daemon_started = True

    def _loop():
        # One-time backfill at boot: protect work built before per-turn persistence.
        try:
            backfill_snapshots(app)
        except Exception:  # noqa: BLE001
            logger.warning("dev snapshot backfill raised", exc_info=True)
        while True:
            time.sleep(DEV_MAINTENANCE_INTERVAL)
            try:
                reap_and_heal_once(app)
            except Exception:  # noqa: BLE001 — the daemon must never die
                logger.warning("dev maintenance pass raised", exc_info=True)

    threading.Thread(target=_loop, name="dev-maintenance", daemon=True).start()
    logger.info("dev maintenance daemon started (interval=%ss)", DEV_MAINTENANCE_INTERVAL)
