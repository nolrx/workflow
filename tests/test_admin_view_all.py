"""
Tests for read-only admin oversight: an admin role can VIEW every user's
projects/apps (session list, App Space, single read) but never MUTATE them.

Network-free: forges JWTs and stubs the runtime; no AI / containers involved.
Covers:
  * session list (`/api/code/projects`): scope=all is admin-only and silently
    ignored for non-admins; admin-all items carry an ``owner`` block.
  * single project read (`GET /projects/<id>`): owner or admin; others 404.
  * writes stay owner-only (admin DELETE of another user's project → 404).
  * App Space (`/api/code/apps`): scope=all is admin-only; detail readable by admin.
"""
import pytest
from flask_jwt_extended import create_access_token

from backend.app import create_app
from backend.extensions import db


@pytest.fixture
def app():
    application = create_app("testing")
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture(autouse=True)
def _no_runtime_start(monkeypatch):
    """Never dispatch a worker when a route starts a run."""
    from backend.services.agent import runtime as rt

    monkeypatch.setattr(rt.agent_runtime, "start", lambda app, run_id: None)


def _make_user(email, role="user", display_name=None):
    from backend.models.user import User

    user = User(email=email, role=role, display_name=display_name or email.split("@")[0])
    db.session.add(user)
    db.session.commit()
    return user


def _make_project(user_id, title="待办应用"):
    from backend.models.code import CodeProject, CodeProjectStatus

    project = CodeProject(
        user_id=user_id,
        title=title,
        requirement_input="做一个待办应用",
        requirements_doc="# 需求\nFR1 可创建/勾选任务。",
        status=CodeProjectStatus.REQUIREMENT_READY,
    )
    db.session.add(project)
    db.session.commit()
    return project


def _make_deployment(project, status="running", health="healthy"):
    from backend.models.code.fullstack import CodeDeployment

    dep = CodeDeployment(
        project_id=project.id,
        user_id=project.user_id,
        status=status,
        health=health,
        api_base_path=f"/app/{project.id}/api",
    )
    db.session.add(dep)
    db.session.commit()
    return dep


def _headers(user_id):
    return {"Authorization": f"Bearer {create_access_token(identity=user_id)}"}


@pytest.fixture
def ctx(app):
    """An admin, a regular owner, and one project+deployment owned by the owner."""
    admin = _make_user("admin@x.com", role="admin", display_name="管理员")
    owner = _make_user("owner@x.com", role="user", display_name="阿强")
    project = _make_project(owner.id, title="阿强的待办")
    _make_deployment(project)
    return {
        "client": app.test_client(),
        "admin": admin.id,
        "owner": owner.id,
        "project_id": project.id,
    }


# --- session list -------------------------------------------------------------
def test_admin_scope_all_lists_other_users_projects(ctx):
    client, pid = ctx["client"], ctx["project_id"]
    resp = client.get("/api/code/projects?scope=all", headers=_headers(ctx["admin"]))
    assert resp.status_code == 200, resp.get_data(as_text=True)
    projects = resp.get_json()["data"]["projects"]
    ids = {p["id"] for p in projects}
    assert pid in ids
    item = next(p for p in projects if p["id"] == pid)
    # Admin-all items are labeled with the owner so the list is attributable.
    assert item["owner"]["display_name"] == "阿强"


def test_admin_without_scope_sees_only_own(ctx):
    client, pid = ctx["client"], ctx["project_id"]
    resp = client.get("/api/code/projects", headers=_headers(ctx["admin"]))
    assert resp.status_code == 200
    ids = {p["id"] for p in resp.get_json()["data"]["projects"]}
    assert pid not in ids  # the project belongs to the owner, not the admin


def test_non_admin_scope_all_is_ignored(ctx):
    """A forged scope=all from a non-admin must NOT widen visibility."""
    client, pid = ctx["client"], ctx["project_id"]
    # A third user with no projects of their own.
    stranger = _make_user("stranger@x.com").id
    resp = client.get("/api/code/projects?scope=all", headers=_headers(stranger))
    assert resp.status_code == 200
    ids = {p["id"] for p in resp.get_json()["data"]["projects"]}
    assert pid not in ids
    # And no owner block leaks for non-admins.
    assert all("owner" not in p for p in resp.get_json()["data"]["projects"])


# --- single project read / write ---------------------------------------------
def test_admin_can_read_other_users_project(ctx):
    client, pid = ctx["client"], ctx["project_id"]
    resp = client.get(f"/api/code/projects/{pid}", headers=_headers(ctx["admin"]))
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert resp.get_json()["data"]["project"]["id"] == pid


def test_non_admin_cannot_read_other_users_project(ctx):
    client, pid = ctx["client"], ctx["project_id"]
    stranger = _make_user("stranger2@x.com").id
    resp = client.get(f"/api/code/projects/{pid}", headers=_headers(stranger))
    assert resp.status_code == 404


def test_admin_cannot_delete_other_users_project(ctx):
    """Read-only oversight: writes stay owner-only even for admins."""
    client, pid = ctx["client"], ctx["project_id"]
    resp = client.delete(f"/api/code/projects/{pid}", headers=_headers(ctx["admin"]))
    assert resp.status_code == 404
    # And the project is still there.
    from backend.models.code import CodeProject

    assert db.session.get(CodeProject, pid) is not None


# --- App Space ----------------------------------------------------------------
def test_admin_scope_all_lists_other_users_apps(ctx):
    client, pid = ctx["client"], ctx["project_id"]
    resp = client.get("/api/code/apps?scope=all", headers=_headers(ctx["admin"]))
    assert resp.status_code == 200, resp.get_data(as_text=True)
    ids = {a["project_id"] for a in resp.get_json()["data"]["apps"]}
    assert pid in ids


def test_non_admin_scope_all_apps_is_ignored(ctx):
    client, pid = ctx["client"], ctx["project_id"]
    stranger = _make_user("stranger3@x.com").id
    resp = client.get("/api/code/apps?scope=all", headers=_headers(stranger))
    assert resp.status_code == 200
    ids = {a["project_id"] for a in resp.get_json()["data"]["apps"]}
    assert pid not in ids


def test_admin_can_read_other_users_app_detail(ctx):
    client, pid = ctx["client"], ctx["project_id"]
    resp = client.get(f"/api/code/apps/{pid}", headers=_headers(ctx["admin"]))
    assert resp.status_code == 200, resp.get_data(as_text=True)
