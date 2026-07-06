"""
Tests for graduating Dev Mode edits to the deployed release.

Answers "现在开发模式调好的应用,怎么更新到最新的发布版本": a deploy must build/serve
from the NEWEST source snapshot (which includes Dev Mode edits), not the original
generation build. Covers:
  * ``deploy_service._resolve_backend_source`` prefers the newest
    ``code_backend_project_zip`` (a Dev Mode backend snapshot) — gated by
    ``CODE_DEPLOY_FROM_DEV`` (off → legacy generation-only behaviour verbatim).
  * ``deploy_service._maybe_build_dev_frontend_dist`` builds a Dev-Mode-tuned frontend
    snapshot into a dist under the deploy run and records ``frontend_site_run_id``;
    skips when nothing was tuned past generation, or when the flag is off.
  * the preview serves the NEWER of the generation dist and the deploy-built dist.

Docker-free: the frontend build container is monkeypatched to a deterministic dist.
"""
import io
import os
import zipfile
from datetime import datetime

import pytest

from backend.app import create_app
from backend.extensions import db


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for rel, content in files.items():
            archive.writestr(rel, content)
    return buf.getvalue()


@pytest.fixture
def app_ctx(tmp_path):
    application = create_app("testing")
    application.config["UPLOAD_FOLDER"] = str(tmp_path / "uploads")
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


def _mk_run(user_id, project_id, workflow, created_at=None):
    from backend.models.agent.run import AgentRun, AgentRunStatus

    run = AgentRun(
        user_id=user_id, domain="code", workflow=workflow,
        resource_type="code_project", resource_id=project_id,
        status=AgentRunStatus.COMPLETED,
    )
    db.session.add(run)
    db.session.commit()
    if created_at:
        run.created_at = created_at
        db.session.commit()
    return run


def _add_zip(run_id, domain_ref_type, project_id, files, created_at=None):
    from backend.models.agent import AgentArtifact, AgentArtifactType
    from backend.services.agent.files import save_artifact_file

    rel = save_artifact_file(run_id, None, "snap.zip", _zip(files))
    art = AgentArtifact(
        run_id=run_id, step_id=None, artifact_type=AgentArtifactType.TEXT,
        title="zip", filename="snap.zip", mime_type="application/zip",
        storage_path=rel, domain_ref_type=domain_ref_type, domain_ref_id=project_id,
    )
    db.session.add(art)
    db.session.commit()
    if created_at:
        art.created_at = created_at
        db.session.commit()
    return art


def _mk_project(user_id):
    from backend.models.code import CodeProject, CodeProjectStatus

    project = CodeProject(
        user_id=user_id, title="T", requirement_input="r", requirements_doc="# 需求",
        style_prompt="s", status=CodeProjectStatus.UI_CONFIRMED,
    )
    db.session.add(project)
    db.session.commit()
    return project


# --- backend source resolution ----------------------------------------------
def test_backend_source_prefers_dev_snapshot(app_ctx, monkeypatch):
    from backend.services.code import deploy_service

    uid, pid = "u1", "p1"
    gen = _mk_run(uid, pid, "code_backend_project_generation")
    _add_zip(gen.id, "code_backend_project_zip", pid, {"app.py": "GEN"}, created_at=datetime(2026, 1, 1))
    dev = _mk_run(uid, pid, "code_dev_backend_turn")
    _add_zip(dev.id, "code_backend_project_zip", pid, {"app.py": "DEV"}, created_at=datetime(2026, 2, 1))

    monkeypatch.setenv("CODE_DEPLOY_FROM_DEV", "1")
    src = deploy_service._resolve_backend_source(pid, uid, gen)
    assert src["app.py"] == b"DEV"


def test_backend_source_flag_off_uses_generation(app_ctx, monkeypatch):
    from backend.services.code import deploy_service

    uid, pid = "u1b", "p1b"
    gen = _mk_run(uid, pid, "code_backend_project_generation")
    _add_zip(gen.id, "code_backend_project_zip", pid, {"app.py": "GEN"}, created_at=datetime(2026, 1, 1))
    dev = _mk_run(uid, pid, "code_dev_backend_turn")
    _add_zip(dev.id, "code_backend_project_zip", pid, {"app.py": "DEV"}, created_at=datetime(2026, 2, 1))

    monkeypatch.setenv("CODE_DEPLOY_FROM_DEV", "0")
    src = deploy_service._resolve_backend_source(pid, uid, gen)
    assert src["app.py"] == b"GEN"


# --- frontend dev-dist build -------------------------------------------------
def test_frontend_dev_dist_built_and_recorded(app_ctx, monkeypatch):
    from backend.services.agent.files import artifact_abs_path
    from backend.services.code import deploy_service, frontend_dist_builder

    uid = "u2"
    project = _mk_project(uid)
    pid = project.id
    gen = _mk_run(uid, pid, "code_frontend_project_generation")
    _add_zip(gen.id, "code_frontend_project_zip", pid, {"src/App.tsx": "GEN"}, created_at=datetime(2026, 1, 1))
    dev = _mk_run(uid, pid, "code_dev_turn")
    _add_zip(dev.id, "code_frontend_project_zip", pid, {"src/App.tsx": "DEV"}, created_at=datetime(2026, 2, 1))
    deploy_run = _mk_run(uid, pid, "code_fullstack_deploy")

    captured = {}

    def fake_build(source, base="/", timeout=None):
        captured["source"] = source
        captured["base"] = base
        return {"index.html": b"<html>DEV</html>", "assets/app.js": b"x"}

    monkeypatch.setattr(frontend_dist_builder, "build_dist", fake_build)
    monkeypatch.setenv("CODE_DEPLOY_FROM_DEV", "1")

    site_run_id = deploy_service._maybe_build_dev_frontend_dist(
        project, uid, gen, deploy_run.id, lambda *a: None
    )
    assert site_run_id == deploy_run.id
    # built from the DEV snapshot, base pinned to the /preview/<pid>/ mount
    assert captured["source"]["src/App.tsx"] == b"DEV"
    assert captured["base"] == f"/preview/{pid}/"
    # dist staged under the deploy run's site dir
    assert os.path.isfile(artifact_abs_path(f"agent_runs/{deploy_run.id}/site/index.html"))


def test_no_dev_change_skips_build(app_ctx, monkeypatch):
    from backend.services.code import deploy_service, frontend_dist_builder

    uid = "u3"
    project = _mk_project(uid)
    pid = project.id
    gen = _mk_run(uid, pid, "code_frontend_project_generation")
    _add_zip(gen.id, "code_frontend_project_zip", pid, {"src/App.tsx": "GEN"}, created_at=datetime(2026, 1, 1))
    deploy_run = _mk_run(uid, pid, "code_fullstack_deploy")

    calls = {"n": 0}

    def fake_build(*a, **k):
        calls["n"] += 1
        return {}

    monkeypatch.setattr(frontend_dist_builder, "build_dist", fake_build)
    monkeypatch.setenv("CODE_DEPLOY_FROM_DEV", "1")

    # Newest frontend zip IS the generation run's own → nothing tuned past generation.
    site = deploy_service._maybe_build_dev_frontend_dist(project, uid, gen, deploy_run.id, lambda *a: None)
    assert site is None
    assert calls["n"] == 0


def test_frontend_flag_off_skips_build(app_ctx, monkeypatch):
    from backend.services.code import deploy_service, frontend_dist_builder

    uid = "u4"
    project = _mk_project(uid)
    pid = project.id
    gen = _mk_run(uid, pid, "code_frontend_project_generation")
    _add_zip(gen.id, "code_frontend_project_zip", pid, {"a": "GEN"}, created_at=datetime(2026, 1, 1))
    dev = _mk_run(uid, pid, "code_dev_turn")
    _add_zip(dev.id, "code_frontend_project_zip", pid, {"a": "DEV"}, created_at=datetime(2026, 2, 1))
    deploy_run = _mk_run(uid, pid, "code_fullstack_deploy")

    monkeypatch.setattr(frontend_dist_builder, "build_dist", lambda *a, **k: {"index.html": b"x"})
    monkeypatch.setenv("CODE_DEPLOY_FROM_DEV", "0")

    assert deploy_service._maybe_build_dev_frontend_dist(project, uid, gen, deploy_run.id, lambda *a: None) is None


def test_build_failure_falls_back(app_ctx, monkeypatch):
    from backend.services.code import deploy_service, frontend_dist_builder

    uid = "u5"
    project = _mk_project(uid)
    pid = project.id
    gen = _mk_run(uid, pid, "code_frontend_project_generation")
    _add_zip(gen.id, "code_frontend_project_zip", pid, {"a": "GEN"}, created_at=datetime(2026, 1, 1))
    dev = _mk_run(uid, pid, "code_dev_turn")
    _add_zip(dev.id, "code_frontend_project_zip", pid, {"a": "DEV"}, created_at=datetime(2026, 2, 1))
    deploy_run = _mk_run(uid, pid, "code_fullstack_deploy")

    # Build produced nothing (docker absent / build error) → keep serving prior dist.
    monkeypatch.setattr(frontend_dist_builder, "build_dist", lambda *a, **k: {})
    monkeypatch.setenv("CODE_DEPLOY_FROM_DEV", "1")

    assert deploy_service._maybe_build_dev_frontend_dist(project, uid, gen, deploy_run.id, lambda *a: None) is None


# --- preview serves the newer dist ------------------------------------------
def test_preview_resolves_newer_deploy_site(app_ctx):
    from backend.routes.code.preview_routes import _resolve_site_dir
    from backend.services.agent.files import agent_run_dir

    uid, pid = "u6", "p6"
    gen = _mk_run(uid, pid, "code_frontend_project_generation", created_at=datetime(2026, 1, 1))
    deploy_run = _mk_run(uid, pid, "code_fullstack_deploy", created_at=datetime(2026, 2, 1))
    (agent_run_dir(gen.id) / "site").mkdir(parents=True, exist_ok=True)
    (agent_run_dir(gen.id) / "site" / "index.html").write_text("GEN")
    (agent_run_dir(deploy_run.id) / "site").mkdir(parents=True, exist_ok=True)
    (agent_run_dir(deploy_run.id) / "site" / "index.html").write_text("DEV")

    class Dep:
        frontend_site_run_id = deploy_run.id

    site = _resolve_site_dir(gen, Dep())
    assert str(site).endswith(f"agent_runs/{deploy_run.id}/site")


def test_preview_falls_back_when_deploy_site_absent(app_ctx):
    from backend.routes.code.preview_routes import _resolve_site_dir
    from backend.services.agent.files import agent_run_dir

    uid, pid = "u7", "p7"
    gen = _mk_run(uid, pid, "code_frontend_project_generation", created_at=datetime(2026, 1, 1))
    deploy_run = _mk_run(uid, pid, "code_fullstack_deploy", created_at=datetime(2026, 2, 1))
    (agent_run_dir(gen.id) / "site").mkdir(parents=True, exist_ok=True)
    (agent_run_dir(gen.id) / "site" / "index.html").write_text("GEN")
    # deploy_run has NO site dir on disk → newest-but-missing → fall back to generation.

    class Dep:
        frontend_site_run_id = deploy_run.id

    site = _resolve_site_dir(gen, Dep())
    assert str(site).endswith(f"agent_runs/{gen.id}/site")


# --- warm dist cache (instant deploy) ---------------------------------------
def test_fresh_dist_cache_hit_skips_cold_build(app_ctx, monkeypatch):
    from backend.services.agent.files import artifact_abs_path
    from backend.services.code import deploy_service, frontend_dist_builder

    uid = "uc1"
    project = _mk_project(uid)
    pid = project.id
    gen = _mk_run(uid, pid, "code_frontend_project_generation")
    _add_zip(gen.id, "code_frontend_project_zip", pid, {"src/App.tsx": "GEN"}, created_at=datetime(2026, 1, 1))
    dev = _mk_run(uid, pid, "code_dev_turn")
    _add_zip(dev.id, "code_frontend_project_zip", pid, {"src/App.tsx": "DEV"}, created_at=datetime(2026, 2, 1))
    # A warm dist cache NEWER than the source snapshot → deploy must use it directly.
    _add_zip(dev.id, "code_frontend_dist_zip", pid,
             {"index.html": "<html>CACHED</html>"}, created_at=datetime(2026, 2, 2))
    deploy_run = _mk_run(uid, pid, "code_fullstack_deploy")

    calls = {"n": 0}
    monkeypatch.setattr(frontend_dist_builder, "build_dist",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or {"x": b"y"})
    monkeypatch.setenv("CODE_DEPLOY_FROM_DEV", "1")

    site = deploy_service._maybe_build_dev_frontend_dist(project, uid, gen, deploy_run.id, lambda *a: None)
    assert site == deploy_run.id
    assert calls["n"] == 0  # cold build NOT invoked — served straight from the cache
    idx = artifact_abs_path(f"agent_runs/{deploy_run.id}/site/index.html")
    assert idx.read_bytes() == b"<html>CACHED</html>"


def test_stale_dist_cache_falls_back_to_cold_build(app_ctx, monkeypatch):
    from backend.services.code import deploy_service, frontend_dist_builder

    uid = "uc2"
    project = _mk_project(uid)
    pid = project.id
    gen = _mk_run(uid, pid, "code_frontend_project_generation")
    _add_zip(gen.id, "code_frontend_project_zip", pid, {"a": "GEN"}, created_at=datetime(2026, 1, 1))
    dev = _mk_run(uid, pid, "code_dev_turn")
    # cache is OLDER than the newest source snapshot → stale, must cold-build.
    _add_zip(dev.id, "code_frontend_dist_zip", pid, {"index.html": "OLD"}, created_at=datetime(2026, 2, 1))
    _add_zip(dev.id, "code_frontend_project_zip", pid, {"a": "DEV"}, created_at=datetime(2026, 2, 5))
    deploy_run = _mk_run(uid, pid, "code_fullstack_deploy")

    calls = {"n": 0}
    monkeypatch.setattr(frontend_dist_builder, "build_dist",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or {"index.html": b"NEW"})
    monkeypatch.setenv("CODE_DEPLOY_FROM_DEV", "1")

    site = deploy_service._maybe_build_dev_frontend_dist(project, uid, gen, deploy_run.id, lambda *a: None)
    assert site == deploy_run.id
    assert calls["n"] == 1  # cold build WAS invoked because the cache was stale


def test_harvest_builds_caches_and_stops(app_ctx, monkeypatch):
    from backend.models.agent import AgentArtifact
    from backend.models.code.fullstack import CodeDevSession, DevSessionStatus
    from backend.services.code import deploy_service

    uid = "uc3"
    project = _mk_project(uid)
    pid = project.id
    gen = _mk_run(uid, pid, "code_frontend_project_generation")
    _add_zip(gen.id, "code_frontend_project_zip", pid, {"a": "GEN"}, created_at=datetime(2026, 1, 1))
    dev = _mk_run(uid, pid, "code_dev_turn")
    _add_zip(dev.id, "code_frontend_project_zip", pid, {"a": "DEV"}, created_at=datetime(2026, 2, 1))
    session = CodeDevSession(
        project_id=pid, user_id=uid, lane="frontend",
        status=DevSessionStatus.RUNNING, container_name=f"dev-{pid[:8]}",
    )
    db.session.add(session)
    db.session.commit()

    stopped = {"n": 0}

    class FakeDev:
        def container_status(self, _pid):
            return {"running": True}

        def build_dist_in_container(self, _pid, base="/"):
            assert base == f"/preview/{pid}/"
            return {"index.html": b"<html>HARVEST</html>"}

        def stop_container(self, _pid):
            stopped["n"] += 1

    # _harvest_dev_frontend_dist imports get_dev_service from dev_service inside the
    # function, so patch it there.
    import backend.services.code.dev_service as dev_service_mod
    monkeypatch.setattr(dev_service_mod, "get_dev_service", lambda: FakeDev())
    monkeypatch.setenv("CODE_DEPLOY_FROM_DEV", "1")

    deploy_service._harvest_dev_frontend_dist(project, uid, gen, lambda *a: None)

    # A warm dist cache was written, and the container/session were torn down.
    cache = (
        AgentArtifact.query.filter_by(domain_ref_type="code_frontend_dist_zip", domain_ref_id=pid).first()
    )
    assert cache is not None
    assert stopped["n"] == 1
    db.session.refresh(session)
    assert session.status == DevSessionStatus.STOPPED


def test_harvest_skips_build_without_dev_edits(app_ctx, monkeypatch):
    from backend.models.agent import AgentArtifact
    from backend.models.code.fullstack import CodeDevSession, DevSessionStatus
    from backend.services.code import deploy_service

    uid = "uc4"
    project = _mk_project(uid)
    pid = project.id
    gen = _mk_run(uid, pid, "code_frontend_project_generation")
    _add_zip(gen.id, "code_frontend_project_zip", pid, {"a": "GEN"}, created_at=datetime(2026, 1, 1))
    session = CodeDevSession(
        project_id=pid, user_id=uid, lane="frontend", status=DevSessionStatus.RUNNING,
    )
    db.session.add(session)
    db.session.commit()

    built = {"n": 0}

    class FakeDev:
        def container_status(self, _pid):
            return {"running": True}

        def build_dist_in_container(self, _pid, base="/"):
            built["n"] += 1
            return {"index.html": b"x"}

        def stop_container(self, _pid):
            pass

    import backend.services.code.dev_service as dev_service_mod
    monkeypatch.setattr(dev_service_mod, "get_dev_service", lambda: FakeDev())
    monkeypatch.setenv("CODE_DEPLOY_FROM_DEV", "1")

    deploy_service._harvest_dev_frontend_dist(project, uid, gen, lambda *a: None)
    # No source tuned past generation → no warm build, but the session is still stopped.
    assert built["n"] == 0
    assert AgentArtifact.query.filter_by(domain_ref_type="code_frontend_dist_zip", domain_ref_id=pid).first() is None
    db.session.refresh(session)
    assert session.status == DevSessionStatus.STOPPED


def test_persist_dist_cache_standalone_creates_artifact(app_ctx):
    from backend.models.agent import AgentArtifact
    from backend.services.agent.workflows.code_dev_turn_workflow import (
        persist_dist_cache_standalone,
    )

    uid, pid = "uc5", "p_uc5"
    run = _mk_run(uid, pid, "code_dev_turn")
    ok = persist_dist_cache_standalone(run.id, pid, {"index.html": b"<html>x</html>"})
    assert ok is True
    art = AgentArtifact.query.filter_by(domain_ref_type="code_frontend_dist_zip", domain_ref_id=pid).first()
    assert art is not None and art.run_id == run.id


def test_preview_route_serves_deploy_built_dist(app_ctx, monkeypatch):
    """End-to-end: a RUNNING deployment whose deploy run built a Dev dist serves it."""
    from flask_jwt_extended import create_access_token

    from backend.models.code.fullstack import CodeDeployment, DeploymentStatus
    from backend.services.agent.files import agent_run_dir

    uid = "u8"
    project = _mk_project(uid)
    pid = project.id
    gen = _mk_run(uid, pid, "code_frontend_project_generation", created_at=datetime(2026, 1, 1))
    deploy_run = _mk_run(uid, pid, "code_fullstack_deploy", created_at=datetime(2026, 2, 1))
    (agent_run_dir(gen.id) / "site").mkdir(parents=True, exist_ok=True)
    (agent_run_dir(gen.id) / "site" / "index.html").write_text(
        "<!doctype html>GEN-MARKER", encoding="utf-8"
    )
    (agent_run_dir(deploy_run.id) / "site").mkdir(parents=True, exist_ok=True)
    (agent_run_dir(deploy_run.id) / "site" / "index.html").write_text(
        "<!doctype html>DEV-MARKER", encoding="utf-8"
    )
    dep = CodeDeployment(
        project_id=pid, user_id=uid, status=DeploymentStatus.RUNNING,
        api_base_path=f"/app/{pid}/api", frontend_site_run_id=deploy_run.id,
    )
    db.session.add(dep)
    db.session.commit()

    client = app_ctx.test_client()
    token = create_access_token(identity=uid)
    resp = client.get(f"/preview/{pid}/?token={token}", follow_redirects=True)
    assert resp.status_code == 200
    assert b"DEV-MARKER" in resp.data
    assert b"GEN-MARKER" not in resp.data
