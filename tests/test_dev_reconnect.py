"""
Tests for Dev Mode reconnection on page refresh.

Bug: refreshing the dev page could not reconnect to the in-flight turn — the resume
POST always spawned a fresh "bootstrap" run that became the latest, HIDING the running
turn, so the user lost all awareness of the execution + its result.

Fix (``dev_routes.start_session`` / ``start_backend_session``): when the container is
still alive, REATTACH to the in-flight (or last) turn run instead of spawning a
bootstrap. These tests pin that a refresh returns the in-flight turn's id and creates
no new run. Docker is monkeypatched (no daemon in CI).
"""
import pytest

from backend.app import create_app
from backend.extensions import db


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    application = create_app("testing")
    application.config["UPLOAD_FOLDER"] = str(tmp_path / "uploads")
    with application.app_context():
        db.create_all()

        # Make Dev Mode "available" + the container look alive, without a docker daemon.
        from backend.services.code.dev_service import DevService

        monkeypatch.setattr(DevService, "is_available", lambda self: True)
        monkeypatch.setattr(
            DevService, "container_status",
            lambda self, pid: {"present": True, "running": True, "restart_count": 0, "state": "running"},
        )
        yield application
        db.session.remove()
        db.drop_all()


def _project(user_id):
    from backend.models.code import CodeProject, CodeProjectStatus

    project = CodeProject(
        user_id=user_id, title="T", requirement_input="r", requirements_doc="# 需求",
        style_prompt="s", status=CodeProjectStatus.UI_CONFIRMED,
    )
    db.session.add(project)
    db.session.commit()
    return project


def _session(project_id, user_id, lane="frontend"):
    from backend.models.code.fullstack import CodeDevSession, DevSessionStatus

    s = CodeDevSession(
        project_id=project_id, user_id=user_id, lane=lane,
        status=DevSessionStatus.RUNNING, container_name=f"dev-{project_id[:8]}",
        internal_port=5173, preview_path=f"/preview/{project_id}/",
    )
    db.session.add(s)
    db.session.commit()
    return s


def _turn_run(project_id, user_id, workflow, status):
    from backend.models.agent.run import AgentRun

    run = AgentRun(
        user_id=user_id, domain="code", workflow=workflow,
        resource_type="code_project", resource_id=project_id, status=status,
    )
    db.session.add(run)
    db.session.commit()
    return run


def _client_and_token(app, user_id):
    from flask_jwt_extended import create_access_token

    return app.test_client(), create_access_token(identity=user_id)


def test_refresh_reattaches_to_inflight_turn(ctx):
    from backend.models.agent.run import AgentRun, AgentRunStatus

    uid = "u-rc1"
    project = _project(uid)
    pid = project.id
    _session(pid, uid)
    inflight = _turn_run(pid, uid, "code_dev_turn", AgentRunStatus.RUNNING)

    before = AgentRun.query.filter_by(resource_id=pid).count()
    client, token = _client_and_token(ctx, uid)
    resp = client.post(
        f"/api/code/projects/{pid}/dev-sessions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.get_json()["data"]
    # Reattached to the in-flight turn — NOT a fresh bootstrap run.
    assert body["run_id"] == inflight.id
    assert AgentRun.query.filter_by(resource_id=pid).count() == before  # no new run spawned


def test_refresh_reattaches_to_last_completed_turn(ctx):
    from backend.models.agent.run import AgentRun, AgentRunStatus

    uid = "u-rc2"
    project = _project(uid)
    pid = project.id
    _session(pid, uid)
    _turn_run(pid, uid, "code_dev_turn", AgentRunStatus.COMPLETED)
    last = _turn_run(pid, uid, "code_dev_turn", AgentRunStatus.COMPLETED)

    before = AgentRun.query.filter_by(resource_id=pid).count()
    client, token = _client_and_token(ctx, uid)
    resp = client.post(
        f"/api/code/projects/{pid}/dev-sessions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.get_json()["data"]
    assert body["run_id"] == last.id  # newest completed turn — replays its result
    assert AgentRun.query.filter_by(resource_id=pid).count() == before


def test_refresh_reattaches_to_inflight_parallel_turn(ctx):
    from backend.models.agent.run import AgentRunStatus

    uid = "u-rc3"
    project = _project(uid)
    pid = project.id
    _session(pid, uid)
    # An older single turn + a newer in-flight PARALLEL turn → reattach to the parallel.
    _turn_run(pid, uid, "code_dev_turn", AgentRunStatus.COMPLETED)
    par = _turn_run(pid, uid, "code_dev_parallel_turn", AgentRunStatus.RUNNING)

    client, token = _client_and_token(ctx, uid)
    resp = client.post(
        f"/api/code/projects/{pid}/dev-sessions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["data"]["run_id"] == par.id


def test_backend_refresh_reattaches_to_inflight_turn(ctx, monkeypatch):
    from backend.models.agent.run import AgentRun, AgentRunStatus
    from backend.services.code.dev_backend_service import DevBackendService

    monkeypatch.setattr(DevBackendService, "is_available", lambda self: True)
    monkeypatch.setattr(
        DevBackendService, "container_status",
        lambda self, pid: {"present": True, "running": True, "restart_count": 0, "state": "running"},
    )
    uid = "u-rc4"
    project = _project(uid)
    pid = project.id
    _session(pid, uid, lane="backend")
    inflight = _turn_run(pid, uid, "code_dev_backend_turn", AgentRunStatus.RUNNING)

    before = AgentRun.query.filter_by(resource_id=pid).count()
    client, token = _client_and_token(ctx, uid)
    resp = client.post(
        f"/api/code/projects/{pid}/dev-backend-sessions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["data"]["run_id"] == inflight.id
    assert AgentRun.query.filter_by(resource_id=pid).count() == before
