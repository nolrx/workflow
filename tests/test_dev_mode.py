"""
Unit tests for Dev Mode (交互式开发模式) — network-free.

Covers the pieces that don't need a live container / docker: the workflow +
pricing registration, the persistent checklist helpers (seed / sync / board with
atomic done-flips), and the session-first ledger reload (design-review must-fix
#4). Container lifecycle (dev_service) is exercised only for its pure helpers.
"""
import uuid

import pytest

from backend.app import create_app
from backend.extensions import db
from backend.models.code import (
    CodeDevSession,
    CodeDevTask,
    CodeProject,
    DevSessionStatus,
    DevTaskStatus,
)
from backend.services.agent.workflows.code_dev_turn_workflow import (
    checklist_board,
    load_dev_ledger,
    seed_checklist,
    sync_checklist,
)
from backend.services.code import dev_sprint_service


@pytest.fixture
def app():
    application = create_app("testing")
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


def _make_project(user_id: str) -> CodeProject:
    project = CodeProject(
        user_id=user_id,
        title="Dev Mode Test",
        requirement_input="做一个任务管理应用",
    )
    db.session.add(project)
    db.session.commit()
    return project


def _make_session(project: CodeProject) -> CodeDevSession:
    session = CodeDevSession(
        project_id=project.id,
        user_id=project.user_id,
        lane="frontend",
        status=DevSessionStatus.RUNNING,
    )
    db.session.add(session)
    db.session.commit()
    return session


_LEDGER = {
    "requirements": [
        {"id": "FR-01", "statement": "用户可以创建任务"},
        {"id": "FR-02", "statement": "用户可以标记任务完成"},
        {"id": "NFR-01", "statement": "首屏加载在 2 秒内"},
    ]
}


# --- registration / pricing --------------------------------------------------
def test_dev_turn_registered(app):
    from backend.routes.agent_routes import WORKFLOW_COSTS
    from backend.services.agent.runtime import get_workflow, known_workflows

    assert "code_dev_turn" in known_workflows()
    assert get_workflow("code_dev_turn") is not None
    assert "code_dev_turn" in WORKFLOW_COSTS


def test_dev_turn_price_default_zero(app):
    from backend.services import pricing

    # Default-off like the rest of the Code domain (env-gated metering).
    assert pricing.CODE_DEV_TURN == 0
    assert pricing.OPERATION["code_dev_turn"] == ("agent_run", 0)


# --- checklist helpers -------------------------------------------------------
def test_seed_checklist_from_ledger(app):
    project = _make_project(str(uuid.uuid4()))
    session = _make_session(project)

    inserted = seed_checklist(session.id, project.id, _LEDGER)
    assert inserted == 3

    board = checklist_board(session.id)
    assert board["total"] == 3
    assert board["functional_total"] == 2  # FR-01, FR-02 (NFR-01 is non-functional)
    assert board["functional_done"] == 0
    ids = {t["feature_id"] for t in board["items"]}
    assert ids == {"FR-01", "FR-02", "NFR-01"}


def test_seed_checklist_idempotent(app):
    project = _make_project(str(uuid.uuid4()))
    session = _make_session(project)
    assert seed_checklist(session.id, project.id, _LEDGER) == 3
    # Re-seeding the same ledger adds nothing (dedupe by feature_id).
    assert seed_checklist(session.id, project.id, _LEDGER) == 0
    # A grown ledger adds only the new feature.
    grown = {"requirements": _LEDGER["requirements"] + [{"id": "FR-03", "statement": "删除任务"}]}
    assert seed_checklist(session.id, project.id, grown) == 1
    assert checklist_board(session.id)["total"] == 4


def test_sync_checklist_flips_done_atomically(app):
    project = _make_project(str(uuid.uuid4()))
    session = _make_session(project)
    seed_checklist(session.id, project.id, _LEDGER)

    # Feature list post apply_feature_results: FR-01 passes, FR-02 not.
    features = [
        {"id": "FR-01", "category": "functional", "description": "创建任务", "passes": True, "note": "已实现"},
        {"id": "FR-02", "category": "functional", "description": "标记完成", "passes": False, "note": ""},
        {"id": "NFR-01", "category": "non_functional", "description": "性能", "passes": False, "note": ""},
    ]
    board = sync_checklist(session.id, project.id, features, run_id="run-1")
    assert board["functional_done"] == 1
    done = {t["feature_id"] for t in board["items"] if t["status"] == DevTaskStatus.DONE}
    assert done == {"FR-01"}

    # A later turn passes FR-02 too; FR-01 stays done (idempotent).
    features[1]["passes"] = True
    board = sync_checklist(session.id, project.id, features, run_id="run-2")
    assert board["functional_done"] == 2


def test_sync_checklist_does_not_resurrect_skipped_parent(app):
    """A coarse FR retired to SKIPPED by auto-decomposition (its children carry the
    work) must NOT be flipped back to DONE by a later audit that judges the FR met."""
    project = _make_project(str(uuid.uuid4()))
    session = _make_session(project)
    seed_checklist(session.id, project.id, _LEDGER)
    # Retire FR-01 as if it were decomposed into children.
    fr01 = next(t for t in dev_sprint_service.session_tasks(session.id) if t.feature_id == "FR-01")
    dev_sprint_service.retire_superseded(fr01.id, "FR-01.T1")
    features = [
        {"id": "FR-01", "category": "functional", "description": "创建任务", "passes": True, "note": "已实现"},
    ]
    sync_checklist(session.id, project.id, features, run_id="run-audit")
    db.session.refresh(fr01)
    assert fr01.status == DevTaskStatus.SKIPPED  # stayed retired, not resurrected to done


def test_sync_checklist_inserts_agent_discovered(app):
    project = _make_project(str(uuid.uuid4()))
    session = _make_session(project)
    seed_checklist(session.id, project.id, _LEDGER)
    # A feature not previously on the board (e.g. requirement grew) is inserted.
    features = [
        {"id": "FR-09", "category": "functional", "description": "新功能", "passes": True, "note": "新增"},
    ]
    board = sync_checklist(session.id, project.id, features, run_id="run-x")
    fr09 = [t for t in board["items"] if t["feature_id"] == "FR-09"]
    assert len(fr09) == 1
    assert fr09[0]["status"] == DevTaskStatus.DONE
    assert fr09[0]["source"] == "agent_discovered"


def test_user_added_task_persists(app):
    project = _make_project(str(uuid.uuid4()))
    session = _make_session(project)
    task = CodeDevTask(
        project_id=project.id, session_id=session.id, feature_id=None,
        category="functional", title="用户手动加的功能", status=DevTaskStatus.PENDING,
        source="user_added", order_index=1,
    )
    db.session.add(task)
    db.session.commit()
    board = checklist_board(session.id)
    assert any(t["title"] == "用户手动加的功能" for t in board["items"])


# --- ledger reload (must-fix #4: session-first, never clobbered) -------------
def test_load_dev_ledger_prefers_session(app):
    project = _make_project(str(uuid.uuid4()))
    session = _make_session(project)
    # The session carries an accumulated ledger with a user decision.
    session.set_shared_ledger({
        "requirements": [{"id": "FR-01", "statement": "会话累积的需求"}],
        "decisions": [{"id": "user-dev-1", "statement": "改用某状态库"}],
    })
    db.session.commit()

    led = load_dev_ledger(session, project)
    reqs = led.to_dict().get("requirements") or []
    assert any(r.get("statement") == "会话累积的需求" for r in reqs)


def test_load_dev_ledger_seeds_when_empty(app):
    project = _make_project(str(uuid.uuid4()))
    session = _make_session(project)
    # No session ledger, no prior runs → seed from the project inputs (non-empty).
    led = load_dev_ledger(session, project)
    assert not led.is_empty()


# --- dev_service pure helpers ------------------------------------------------
def test_dev_container_name_stable_and_bounded(app):
    from backend.services.code.dev_service import _dev_container_name

    pid = "11111111-2222-3333-4444-555555555555"
    name = _dev_container_name(pid)
    assert name.startswith("dev-")
    assert len(name) <= 60
    assert _dev_container_name(pid) == name  # deterministic


def test_dev_entrypoint_has_proxy_and_viteconfig(app):
    from backend.services.code.dev_service import _DEV_ENTRYPOINT

    # must-fix #2: the retry proxy is spliced into the entrypoint (persists for the
    # container's life); the merged Vite config fixes base/allowedHosts/hmr.
    assert "anthropic-proxy.mjs" in _DEV_ENTRYPOINT
    assert "vite.dev-mode.config.mjs" in _DEV_ENTRYPOINT
    assert "allowedHosts" in _DEV_ENTRYPOINT
    # parallel prerequisite: a git baseline is established at container start.
    assert 'git -C "$ROOT"' in _DEV_ENTRYPOINT


# --- parallel multi-subagent development -------------------------------------
def test_dev_parallel_turn_registered(app):
    from backend.routes.agent_routes import WORKFLOW_COSTS
    from backend.services.agent.runtime import get_workflow, known_workflows

    assert "code_dev_parallel_turn" in known_workflows()
    assert get_workflow("code_dev_parallel_turn") is not None
    assert "code_dev_parallel_turn" in WORKFLOW_COSTS


class _FakeDev:
    """A docker-free stand-in for DevService that records orchestration calls."""

    def __init__(self, *, git=True, merge_ok=True):
        self._git = git
        self._merge_ok = merge_ok
        self.exec_workdirs: list = []
        self.calls: list = []

    def is_available(self):
        return True

    def container_status(self, pid):
        return {"present": True, "running": True, "restart_count": 0}

    def health_check(self, pid):
        return True

    def git_ready(self, pid):
        return self._git

    def create_worktree(self, pid, i):
        self.calls.append(("worktree", i))
        return f"/work-lanes/lane-{i}"

    def exec_turn(self, pid, prompt, on_event=None, is_cancelled=None, workdir=None, timeout=None):
        from backend.services.code.dev_service import DevTurnResult

        self.exec_workdirs.append(workdir)
        if on_event:
            on_event({
                "type": "assistant",
                "message": {"content": [
                    {"type": "tool_use", "name": "Write", "input": {"file_path": "src/x.tsx"}}
                ]},
            })
        return DevTurnResult(True)

    def commit_worktree(self, pid, i):
        self.calls.append(("commit", i))

    def merge_lane(self, pid, i):
        self.calls.append(("merge", i))
        return (True, []) if self._merge_ok else (False, ["src/App.tsx"])

    def cleanup_worktrees(self, pid, ids):
        self.calls.append(("cleanup", tuple(ids)))

    def collect_source(self, pid):
        return {"src/x.tsx": b"export {}\n"}


class _FakeService:
    def review_project(self, **kw):
        return None


def _run_parallel(app, monkeypatch, lanes, fake):
    from backend.models.agent import AgentRun, AgentRunStatus
    from backend.services.agent.recorder import RunRecorder
    from backend.services.agent.schemas import AgentContext
    from backend.services.agent.workflows import code_dev_parallel_turn_workflow as wf

    monkeypatch.setattr(wf, "get_dev_service", lambda: fake)
    monkeypatch.setattr(wf, "get_frontend_project_service", lambda: _FakeService())

    uid = str(uuid.uuid4())
    project = _make_project(uid)
    session = _make_session(project)
    run = AgentRun(
        user_id=uid, domain="code", workflow="code_dev_parallel_turn",
        resource_type="code_project", resource_id=project.id,
        status=AgentRunStatus.RUNNING, credit_reserved=0,
    )
    run.set_config({"session_id": session.id, "lanes": [{"instruction": t} for t in lanes]})
    db.session.add(run)
    db.session.commit()
    ctx = AgentContext(
        run_id=run.id, user_id=uid, team_id=None, domain="code",
        workflow="code_dev_parallel_turn", resource_type="code_project",
        resource_id=project.id, config=run.get_config(), input_snapshot={},
    )
    result = wf.run_code_dev_parallel_turn_workflow(ctx, RunRecorder(run.id))
    return result, fake


def test_parallel_uses_isolated_worktrees(app, monkeypatch):
    from backend.models.agent import AgentRunStatus

    result, fake = _run_parallel(app, monkeypatch, ["功能A", "功能B"], _FakeDev(git=True))
    assert result["status"] == AgentRunStatus.COMPLETED
    # Both lanes edited their OWN worktree (parallel isolation).
    assert set(fake.exec_workdirs) == {"/work-lanes/lane-0", "/work-lanes/lane-1"}
    # Integration barrier: each lane committed + merged, then worktrees cleaned up.
    assert ("merge", 0) in fake.calls and ("merge", 1) in fake.calls
    assert any(c[0] == "cleanup" for c in fake.calls)


def test_parallel_serial_fallback_without_git(app, monkeypatch):
    from backend.models.agent import AgentRunStatus

    result, fake = _run_parallel(app, monkeypatch, ["功能A", "功能B"], _FakeDev(git=False))
    assert result["status"] == AgentRunStatus.COMPLETED
    # No git → serial on /work (workdir None), no worktrees / merges.
    assert fake.exec_workdirs == [None, None]
    assert not any(c[0] in ("worktree", "merge") for c in fake.calls)


def test_parallel_conflict_falls_back_to_serial_reapply(app, monkeypatch):
    from backend.models.agent import AgentRunStatus

    result, fake = _run_parallel(app, monkeypatch, ["功能A", "功能B"], _FakeDev(git=True, merge_ok=False))
    assert result["status"] == AgentRunStatus.COMPLETED
    # Parallel edit in worktrees, merges conflict → both re-applied serially on /work.
    assert "/work-lanes/lane-0" in fake.exec_workdirs
    assert fake.exec_workdirs.count(None) == 2  # both lanes re-applied serially


def test_worktree_git_commands(app, monkeypatch):
    """create/merge/cleanup build the expected git commands (no real docker)."""
    import types

    from backend.services.code import dev_service

    captured = {}

    def fake_docker(args, timeout):
        captured["script"] = args[-1]  # bash -lc <script>
        return types.SimpleNamespace(returncode=0, stdout="OK MERGED", stderr="")

    monkeypatch.setattr(dev_service, "_docker", fake_docker)
    svc = dev_service.get_dev_service()

    svc.create_worktree("pid-123", 2)
    assert "git worktree add" in captured["script"] and "dev-lane-2" in captured["script"]

    ok, conflicts = svc.merge_lane("pid-123", 1)
    assert "git merge --no-edit" in captured["script"] and "dev-lane-1" in captured["script"]
    assert ok is True and conflicts == []


# --- runtime hardening: maintenance (idle reap + crash self-heal) ------------
class _MaintFakeDev:
    def __init__(self, present=True, running=True, start_ok=True, restart_count=0):
        self.present, self.running, self.start_ok = present, running, start_ok
        self.restart_count = restart_count
        self.stopped = self.started = False

    def container_status(self, pid):
        return {
            "present": self.present, "running": self.running,
            "restart_count": self.restart_count,
        }

    def health_check(self, pid):
        return self.running

    def stop_container(self, pid):
        self.stopped = True

    def start_container(self, pid, source):
        self.started = True
        if self.start_ok:
            return True, None, {
                "container_name": "dev-x", "internal_port": 5173,
                "workdir": "/w", "preview_path": f"/preview/{pid}/",
            }
        return False, "boom", {}


def test_reconcile_session_marks_stopped_when_gone(app):
    from backend.services.code.dev_maintenance import reconcile_session

    project = _make_project(str(uuid.uuid4()))
    session = _make_session(project)  # RUNNING
    changed = reconcile_session(session, _MaintFakeDev(present=False))
    assert changed is True
    assert session.status == DevSessionStatus.STOPPED


def test_reconcile_session_promotes_starting_to_running(app):
    from backend.services.code.dev_maintenance import reconcile_session

    project = _make_project(str(uuid.uuid4()))
    session = _make_session(project)
    session.status = DevSessionStatus.STARTING
    db.session.commit()
    changed = reconcile_session(session, _MaintFakeDev(present=True, running=True))
    assert changed is True
    assert session.status == DevSessionStatus.RUNNING
    assert session.health == "healthy"


def test_reap_idle_session(app, monkeypatch):
    from datetime import datetime, timedelta

    from backend.services.code import dev_maintenance
    from backend.services.code.dev_service import DEV_IDLE_REAP_SECONDS

    project = _make_project(str(uuid.uuid4()))
    session = _make_session(project)
    session.last_active_at = datetime.utcnow() - timedelta(seconds=DEV_IDLE_REAP_SECONDS + 100)
    db.session.commit()
    fake = _MaintFakeDev()
    monkeypatch.setattr(dev_maintenance, "get_dev_service", lambda: fake)

    result = dev_maintenance.reap_and_heal_once(app)
    assert result["reaped"] == 1
    assert fake.stopped is True
    db.session.refresh(session)
    assert session.status == DevSessionStatus.STOPPED


def test_heal_crashed_session(app, monkeypatch):
    from backend.services.code import dev_maintenance

    project = _make_project(str(uuid.uuid4()))
    session = _make_session(project)  # RUNNING, last_active now (not idle)
    fake = _MaintFakeDev(present=False, start_ok=True)  # container vanished
    monkeypatch.setattr(dev_maintenance, "get_dev_service", lambda: fake)

    result = dev_maintenance.reap_and_heal_once(app)
    assert result["healed"] == 1
    assert fake.started is True
    db.session.refresh(session)
    assert session.status == DevSessionStatus.RUNNING
    assert session.get_detail().get("heal_count") == 1


def test_heal_gives_up_after_max(app, monkeypatch):
    from backend.services.code import dev_maintenance
    from backend.services.code.dev_maintenance import DEV_MAX_HEAL

    project = _make_project(str(uuid.uuid4()))
    session = _make_session(project)
    session.set_detail({"heal_count": DEV_MAX_HEAL})
    db.session.commit()
    fake = _MaintFakeDev(present=False)
    monkeypatch.setattr(dev_maintenance, "get_dev_service", lambda: fake)

    dev_maintenance.reap_and_heal_once(app)
    db.session.refresh(session)
    assert session.status == DevSessionStatus.FAILED
    assert fake.started is False  # never attempted past the cap
    assert fake.stopped is True  # gave up → container stopped (no endless --restart loop)


def test_heal_crashlooping_container(app, monkeypatch):
    """A present-but-crash-looping container (e.g. stale entrypoint after a code fix)
    is re-created from current code, not left kernel-restarting forever."""
    from backend.services.code import dev_maintenance
    from backend.services.code.dev_maintenance import DEV_CRASHLOOP_RESTARTS

    project = _make_project(str(uuid.uuid4()))
    session = _make_session(project)  # RUNNING, not idle
    fake = _MaintFakeDev(present=True, running=False, restart_count=DEV_CRASHLOOP_RESTARTS + 2)
    monkeypatch.setattr(dev_maintenance, "get_dev_service", lambda: fake)

    result = dev_maintenance.reap_and_heal_once(app)
    assert result["healed"] == 1
    assert fake.started is True  # re-created from current code
    db.session.refresh(session)
    assert session.status == DevSessionStatus.RUNNING


def test_is_runnable_vite_gates_scaffold_fallback(app):
    """A non-Vite / scaffold-only prior artifact must NOT be treated as runnable
    (it would crash-loop npm run dev) — the caller then uses the minimal scaffold."""
    import json as _json

    from backend.services.agent.workflows.code_dev_turn_workflow import (
        _MINIMAL_SCAFFOLD,
        is_runnable_vite,
    )

    # The bundled minimal scaffold IS a runnable Vite app.
    assert is_runnable_vite(dict(_MINIMAL_SCAFFOLD)) is True
    # Empty → not runnable.
    assert is_runnable_vite({}) is False
    # Scaffold-only stub (real 3a3223 shape: package.json w/o vite, no src/) → not runnable.
    stub = {
        "package.json": _json.dumps({"name": "x", "scripts": {"build": "x"}, "dependencies": {}}).encode(),
        "index.html": b"<html></html>",
        "AGENTS.md": b"# docs",
        "docs/contract.md": b"x",
    }
    assert is_runnable_vite(stub) is False
    # A genuine Vite project (vite devDep + a source module) → runnable, kept as-is.
    real = {
        "package.json": _json.dumps(
            {"scripts": {"dev": "vite"}, "devDependencies": {"vite": "^5.0.0"}}
        ).encode(),
        "src/main.tsx": b"import React from 'react'",
    }
    assert is_runnable_vite(real) is True


def test_audit_calibrates_checklist_over_existing_code(app, monkeypatch):
    """A bootstrap turn with audit=True (existing runnable code) runs the acceptance
    review against the current repo and calibrates the checklist to what's already
    implemented — no instruction, no edit, no repair."""
    from backend.models.agent import AgentRun, AgentRunStatus
    from backend.services.agent.recorder import RunRecorder
    from backend.services.agent.schemas import AgentContext
    from backend.services.agent.workflows import code_dev_turn_workflow as wf

    project = _make_project(str(uuid.uuid4()))
    session = _make_session(project)
    session.set_shared_ledger({"requirements": [
        {"id": "FR-01", "statement": "创建任务"},
        {"id": "FR-02", "statement": "删除任务"},
    ]})
    db.session.commit()

    class _AuditFakeDev:
        def is_available(self):
            return True

        def container_status(self, pid):
            return {"present": True, "running": True, "restart_count": 0}

        def health_check(self, pid):
            return True

        def collect_source(self, pid):
            return {"src/App.tsx": b"export default function App(){return null}"}

    class _AuditFakeService:
        called = {}

        def review_project(self, **kw):
            _AuditFakeService.called = kw
            return {"verdict": "pass", "feature_results": [
                {"id": "FR-01", "passes": True, "note": "已实现"},
                {"id": "FR-02", "passes": False, "note": "缺失"},
            ]}

    monkeypatch.setattr(wf, "get_dev_service", lambda: _AuditFakeDev())
    monkeypatch.setattr(wf, "get_frontend_project_service", lambda: _AuditFakeService())

    run = AgentRun(
        user_id=project.user_id, domain="code", workflow="code_dev_turn",
        resource_type="code_project", resource_id=project.id,
        status=AgentRunStatus.RUNNING, credit_reserved=0,
    )
    run.set_config({"session_id": session.id, "instruction": "", "bootstrap": True, "audit": True})
    db.session.add(run)
    db.session.commit()
    ctx = AgentContext(
        run_id=run.id, user_id=project.user_id, team_id=None, domain="code",
        workflow="code_dev_turn", resource_type="code_project", resource_id=project.id,
        config=run.get_config(), input_snapshot={},
    )
    result = wf.run_code_dev_turn_workflow(ctx, RunRecorder(run.id))
    assert result["status"] == AgentRunStatus.COMPLETED
    # The review ran (audit) even without an instruction, and calibrated the board.
    assert _AuditFakeService.called, "acceptance review should run in audit mode"
    board = checklist_board(session.id)
    done = {t["feature_id"] for t in board["items"] if t["status"] == DevTaskStatus.DONE}
    assert "FR-01" in done and "FR-02" not in done


def test_dev_source_snapshot_zip_and_artifact(app):
    """The work is snapshotted as a code_frontend_project_zip artifact so a re-entry
    restores it (the container fs is destroyed on stop)."""
    import io as _io
    import zipfile as _zip

    from backend.services.agent.workflows.code_dev_turn_workflow import (
        _zip_source,
        persist_source_snapshot,
    )

    files = {"package.json": b'{"scripts":{"dev":"vite"}}', "src/main.js": b"console.log(1)"}
    blob = _zip_source(files)
    with _zip.ZipFile(_io.BytesIO(blob)) as arc:
        assert arc.read("src/main.js") == b"console.log(1)"
        assert "package.json" in arc.namelist()

    # persist writes a `code_frontend_project_zip` artifact (what load_prior_source reads).
    calls = []

    class _FakeStep:
        def add_artifact(self, *a, **kw):
            calls.append(kw)

    persist_source_snapshot(_FakeStep(), "pid-x", files)
    assert calls and calls[0].get("domain_ref_type") == "code_frontend_project_zip"
    assert calls[0].get("content_bytes")
    # An empty source is a no-op (no artifact).
    calls.clear()
    persist_source_snapshot(_FakeStep(), "pid-x", {})
    assert not calls


def test_is_restart_trigger(app):
    from backend.services.agent.workflows.code_dev_turn_workflow import _is_restart_trigger

    # Universal, framework-agnostic (not just Vite): manifests/lockfiles, every common
    # dev-server / build config, CSS pipeline, TS config, env files.
    for f in [
        "package.json", "/tmp/work/vite.config.ts", "vite.config.js", "package-lock.json",
        "pnpm-lock.yaml", "yarn.lock", "next.config.mjs", "svelte.config.js",
        "vue.config.js", "astro.config.mjs", "nuxt.config.ts", "tailwind.config.js",
        "postcss.config.cjs", "tsconfig.json", "jsconfig.json", ".env", ".env.local",
        "app/.env.production",
    ]:
        assert _is_restart_trigger(f) is True, f
    for f in ["src/App.tsx", "src/game/GameEngine.js", "index.html", "src/env.ts", ""]:
        assert _is_restart_trigger(f) is False, f


def test_backfill_snapshots_running_sessions(app, monkeypatch):
    """Boot backfill snapshots EVERY running dev session (universal, not one project),
    so work built before per-turn persistence was in place isn't lost on stop/reap."""
    from backend.models.agent import AgentRun, AgentRunStatus
    from backend.services.agent.workflows import code_dev_turn_workflow as wf
    from backend.services.code import dev_maintenance

    project = _make_project(str(uuid.uuid4()))
    _make_session(project)  # RUNNING
    run = AgentRun(
        user_id=project.user_id, domain="code", workflow="code_dev_turn",
        resource_type="code_project", resource_id=project.id,
        status=AgentRunStatus.COMPLETED, credit_reserved=0,
    )
    db.session.add(run)
    db.session.commit()

    class _BackfillFakeDev:
        def container_status(self, pid):
            return {"present": True, "running": True, "restart_count": 0}

        def collect_source(self, pid):
            return {"src/main.js": b"// work"}

    monkeypatch.setattr(dev_maintenance, "get_dev_service", lambda: _BackfillFakeDev())
    persisted = []
    monkeypatch.setattr(
        wf, "persist_snapshot_standalone",
        lambda run_id, pid, files: (persisted.append((run_id, pid)) or True),
    )

    assert dev_maintenance.backfill_snapshots(app) == 1
    assert persisted and persisted[0][1] == project.id


def test_config_change_triggers_dev_server_restart(app, monkeypatch):
    """A turn that writes package.json / vite.config restarts the dev server so Vite
    reloads its config + installs new deps (a stale plugin would 500 otherwise)."""
    from backend.models.agent import AgentRun, AgentRunStatus
    from backend.services.agent.recorder import RunRecorder
    from backend.services.agent.schemas import AgentContext
    from backend.services.agent.workflows import code_dev_turn_workflow as wf
    from backend.services.code.dev_service import DevTurnResult

    project = _make_project(str(uuid.uuid4()))
    session = _make_session(project)
    session.set_shared_ledger({"requirements": [{"id": "FR-01", "statement": "x"}]})
    db.session.commit()

    class _CfgFakeDev:
        restarted: list = []

        def is_available(self):
            return True

        def container_status(self, pid):
            return {"present": True, "running": True, "restart_count": 0}

        def health_check(self, pid):
            return True

        def collect_source(self, pid):
            return {"src/main.js": b"// vanilla"}

        def exec_turn(self, pid, prompt, on_event=None, is_cancelled=None, workdir=None, timeout=None):
            if on_event:
                on_event({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Write", "input": {"file_path": "package.json"}},
                ]}})
            return DevTurnResult(True)

        def restart_dev_server(self, pid, wait=True):
            _CfgFakeDev.restarted.append(pid)
            return True

    class _CfgFakeService:
        def review_project(self, **kw):
            return None

    monkeypatch.setattr(wf, "get_dev_service", lambda: _CfgFakeDev())
    monkeypatch.setattr(wf, "get_frontend_project_service", lambda: _CfgFakeService())
    monkeypatch.setattr(wf, "_DEV_REPAIR", False)  # isolate: no repair round

    run = AgentRun(
        user_id=project.user_id, domain="code", workflow="code_dev_turn",
        resource_type="code_project", resource_id=project.id,
        status=AgentRunStatus.RUNNING, credit_reserved=0,
    )
    run.set_config({"session_id": session.id, "instruction": "把项目改成 vanilla JS,去掉 React"})
    db.session.add(run)
    db.session.commit()
    ctx = AgentContext(
        run_id=run.id, user_id=project.user_id, team_id=None, domain="code",
        workflow="code_dev_turn", resource_type="code_project", resource_id=project.id,
        config=run.get_config(), input_snapshot={},
    )
    result = wf.run_code_dev_turn_workflow(ctx, RunRecorder(run.id))
    assert result["status"] == AgentRunStatus.COMPLETED
    assert _CfgFakeDev.restarted == [project.id]  # package.json write → one restart


def test_ledger_decision_dedup_and_render_cap(app):
    from backend.services.agent.context_ledger import _MAX_RENDER_DECISIONS, seed_from_inputs

    led = seed_from_inputs("做个应用", "T", None)
    # Exact-duplicate revisions on the same stage are folded, not accumulated.
    led.record_user_revision("dev", "改用深色主题")
    led.record_user_revision("dev", "改用深色主题")
    dev_decisions = [d for d in led.to_dict()["decisions"] if str(d["id"]).startswith("user-dev-")]
    assert len(dev_decisions) == 1
    # The render caps the decisions section (most recent kept) so it can't starve.
    for i in range(_MAX_RENDER_DECISIONS + 10):
        led.record_user_revision("dev", f"决策变体 {i}")
    rendered = led.render_for_prompt(max_chars=100000)
    assert "决策略" in rendered  # elision marker present when over the cap


# --- backend dev mode (full-stack loop) --------------------------------------
def test_dev_backend_turn_registered(app):
    from backend.routes.agent_routes import WORKFLOW_COSTS
    from backend.services.agent.runtime import get_workflow, known_workflows

    assert "code_dev_backend_turn" in known_workflows()
    assert get_workflow("code_dev_backend_turn") is not None
    assert "code_dev_backend_turn" in WORKFLOW_COSTS


def test_dev_be_container_name_stable_and_bounded(app):
    from backend.services.code.dev_backend_service import _dev_be_container_name

    name = _dev_be_container_name("3a3223e2-f486-4da6-bed7-b05028730996")
    assert name.startswith("dev-be-")
    assert len(name) <= 60
    # Distinct from the frontend dev container + the deploy container.
    from backend.services.code.dev_service import _dev_container_name

    assert _dev_be_container_name("x-y-z") != _dev_container_name("x-y-z")


def test_dev_be_db_key_isolated(app):
    """The dev database namespace must NOT collide with the deploy namespace, so dev
    experimentation never reads/writes a live deployed app's data."""
    from backend.services.code.dev_backend_service import _dev_db_key
    from backend.services.code.middleware_service import _sanitized_db_name

    pid = "3a3223e2-f486-4da6-bed7-b05028730996"
    assert _dev_db_key(pid) != pid
    assert _sanitized_db_name(_dev_db_key(pid)) != _sanitized_db_name(pid)


def test_dev_be_entrypoint_covers_polyglot(app):
    from backend.services.code.dev_backend_service import _DEV_BE_ENTRYPOINT

    # Retry proxy spliced in (reliable exec'd claude), seed-once guard, a project
    # dev-start.sh always wins, and each stack's hot-reload runner is present.
    assert "ANTHROPIC" in _DEV_BE_ENTRYPOINT
    assert ".seeded" in _DEV_BE_ENTRYPOINT
    assert "dev-start.sh" in _DEV_BE_ENTRYPOINT
    for token in ("uvicorn", "flask", "nodemon", "go build", "spring-boot:run"):
        assert token in _DEV_BE_ENTRYPOINT, token
    # Placeholder /health server keeps the container up when nothing is runnable yet.
    assert "placeholder" in _DEV_BE_ENTRYPOINT


def test_minimal_backend_scaffold_runnable(app):
    import json as _json

    from backend.services.code.dev_backend_service import _MINIMAL_BACKEND_SCAFFOLD

    assert set(_MINIMAL_BACKEND_SCAFFOLD) >= {"package.json", "server.js", "dev-start.sh"}
    pkg = _json.loads(_MINIMAL_BACKEND_SCAFFOLD["package.json"].decode())
    assert "dev" in pkg.get("scripts", {})
    server = _MINIMAL_BACKEND_SCAFFOLD["server.js"].decode()
    assert "/health" in server and "process.env.PORT" in server


def test_is_be_restart_trigger(app):
    from backend.services.agent.workflows.code_dev_backend_turn_workflow import (
        _is_be_restart_trigger,
    )

    for f in [
        "dev-start.sh", "package.json", "requirements.txt", "pyproject.toml",
        "go.mod", "go.sum", "pom.xml", "build.gradle", "Dockerfile", ".env",
        ".env.production", "app/requirements.txt",
    ]:
        assert _is_be_restart_trigger(f) is True, f
    for f in ["server.js", "src/routes/users.py", "main.go", "README.md", ""]:
        assert _is_be_restart_trigger(f) is False, f


def test_resolve_backend_source_scaffolds_when_empty(app, monkeypatch):
    import backend.services.agent.workflows._iteration_support as it
    from backend.services.agent.workflows import code_dev_backend_turn_workflow as wf
    from backend.services.code.dev_backend_service import _MINIMAL_BACKEND_SCAFFOLD

    # load_prior_source is imported lazily from _iteration_support → patch it there.
    monkeypatch.setattr(it, "load_prior_source", lambda pid, lane: {})
    src = wf._resolve_backend_source(str(uuid.uuid4()))
    assert set(src) == set(_MINIMAL_BACKEND_SCAFFOLD)


def test_dev_maintenance_picks_service_by_lane(app):
    from backend.services.code.dev_backend_service import DevBackendService
    from backend.services.code.dev_maintenance import _dev_for
    from backend.services.code.dev_service import DevService

    project = _make_project(str(uuid.uuid4()))
    fe = CodeDevSession(project_id=project.id, user_id=project.user_id, lane="frontend",
                        status=DevSessionStatus.RUNNING)
    be = CodeDevSession(project_id=project.id, user_id=project.user_id, lane="backend",
                        status=DevSessionStatus.RUNNING)
    db.session.add_all([fe, be])
    db.session.commit()
    assert isinstance(_dev_for(fe), DevService)
    assert isinstance(_dev_for(be), DevBackendService)


class _FakeBackendDev:
    """Docker-free stand-in for DevBackendService."""

    def __init__(self):
        self.restarted = False
        self.turns = 0

    def is_available(self):
        return True

    def container_status(self, pid):
        return {"present": True, "running": True, "restart_count": 0}

    def wait_ready(self, pid, timeout=None):
        return True

    def health_check(self, pid):
        return True

    def restart_dev_server(self, pid, wait=True):
        self.restarted = True
        return True

    def container_logs(self, pid, tail=200):
        return ""

    def start_container(self, pid, source):
        return True, None, {"container_name": "dev-be-x", "internal_port": 8080, "workdir": "/tmp/x"}

    def stop_container(self, pid):
        pass

    def exec_turn(self, pid, prompt, on_event=None, is_cancelled=None, workdir=None, timeout=None):
        from backend.services.code.dev_backend_service import DevTurnResult

        self.turns += 1
        if on_event:
            on_event({
                "type": "assistant",
                "message": {"content": [
                    {"type": "tool_use", "name": "Write", "input": {"file_path": "package.json"}}
                ]},
            })
        return DevTurnResult(True)

    def collect_source(self, pid):
        return {"server.js": b"// api\n", "package.json": b"{}\n"}


class _FakeBackendService:
    def review_project(self, **kw):
        return None


def _run_backend_turn(app, monkeypatch, cfg, fake):
    from backend.models.agent import AgentArtifact, AgentRun, AgentRunStatus
    from backend.services.agent.recorder import RunRecorder
    from backend.services.agent.schemas import AgentContext
    from backend.services.agent.workflows import code_dev_backend_turn_workflow as wf
    from backend.services.code.fullstack import integration_test_service

    monkeypatch.setattr(wf, "get_dev_backend_service", lambda: fake)
    monkeypatch.setattr(wf, "get_backend_project_service", lambda: _FakeBackendService())
    itest_calls = []
    monkeypatch.setattr(
        integration_test_service, "run_integration_tests",
        lambda **kw: (itest_calls.append(kw) or {
            "gate": "pass", "reason": "", "summary": {"failed": []}, "cases": [], "plan": []}),
    )

    uid = str(uuid.uuid4())
    project = _make_project(uid)
    session = CodeDevSession(
        project_id=project.id, user_id=uid, lane="backend",
        status=DevSessionStatus.RUNNING, preview_path=f"/preview/{project.id}/api",
        container_name="dev-be-x", internal_port=8080,
    )
    db.session.add(session)
    db.session.commit()
    # Leave shared_ledger empty → the workflow seeds it from the project inputs.

    run = AgentRun(
        user_id=uid, domain="code", workflow="code_dev_backend_turn",
        resource_type="code_project", resource_id=project.id,
        status=AgentRunStatus.RUNNING, credit_reserved=0,
    )
    run.set_config({"session_id": session.id, **cfg})
    db.session.add(run)
    db.session.commit()
    ctx = AgentContext(
        run_id=run.id, user_id=uid, team_id=None, domain="code",
        workflow="code_dev_backend_turn", resource_type="code_project",
        resource_id=project.id, config=run.get_config(), input_snapshot={},
    )
    result = wf.run_code_dev_backend_turn_workflow(ctx, RunRecorder(run.id))
    arts = AgentArtifact.query.filter_by(
        domain_ref_type="code_backend_project_zip", domain_ref_id=project.id).all()
    return result, session, itest_calls, arts


def test_backend_turn_edit_verifies_and_snapshots(app, monkeypatch):
    from backend.models.agent import AgentRunStatus

    fake = _FakeBackendDev()
    result, session, itest_calls, arts = _run_backend_turn(
        app, monkeypatch, {"instruction": "实现登录接口"}, fake)

    assert result["status"] == AgentRunStatus.COMPLETED
    assert fake.turns >= 1
    # A package.json write triggered a dev-server restart (deps changed).
    assert fake.restarted is True
    # The contract integration test ran against the dev backend container.
    assert itest_calls and itest_calls[0]["container"] == "dev-be-x"
    # The work was snapshotted as a backend project zip (re-entry restores it).
    assert len(arts) == 1


def test_backend_run_tests_only_turn(app, monkeypatch):
    """A test-only turn (no edit) still runs the integration test + no restart."""
    from backend.models.agent import AgentRunStatus

    fake = _FakeBackendDev()
    result, session, itest_calls, arts = _run_backend_turn(
        app, monkeypatch, {"instruction": "", "run_tests": True}, fake)

    assert result["status"] == AgentRunStatus.COMPLETED
    assert fake.turns == 0  # no edit round
    assert itest_calls  # itest still ran


def test_backend_container_logs_merges_and_clamps(app, monkeypatch):
    """container_logs shows ALL types (stdout+stderr merged), timestamped, tail-clamped."""
    import types

    from backend.services.code import dev_backend_service

    captured = {}

    def fake_docker(args, timeout):
        captured["args"] = args
        return types.SimpleNamespace(returncode=0, stdout="OUT\n", stderr="ERR\n")

    monkeypatch.setattr(dev_backend_service, "_docker", fake_docker)
    svc = dev_backend_service.get_dev_backend_service()
    out = svc.container_logs("p-1", tail=99999, timestamps=True)

    assert "OUT" in out and "ERR" in out  # merged stdout + stderr
    assert "--timestamps" in captured["args"]
    # tail clamped to the 2000 ceiling.
    i = captured["args"].index("--tail")
    assert captured["args"][i + 1] == "2000"
