"""
HTTP route-level tests for the remix canvas, mirroring the frontend's calls.

Uses Flask's test_client with a forged JWT (no real login/AI/servers) to exercise
exactly the endpoints the canvas UI hits: canvas CRUD (loadForProject) and the
code_canvas_generation run creation (runCanvas). This catches route-layer 500s
that the workflow-level unit tests can't.
"""
import pytest

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


@pytest.fixture
def auth(app):
    """A project owned by a user, plus that user's bearer header."""
    from flask_jwt_extended import create_access_token

    from backend.models.code import CodeProject, CodeProjectStatus

    user_id = "u-canvas-test"
    project = CodeProject(
        user_id=user_id,
        title="待办应用",
        requirement_input="做一个待办应用",
        requirements_doc="# 需求\n可勾选清单",
        development_flow="# 流程\n增删改",
        style_prompt="简洁",
        status=CodeProjectStatus.REQUIREMENT_READY,
    )
    db.session.add(project)
    db.session.commit()
    token = create_access_token(identity=user_id)
    return {
        "client": app.test_client(),
        "headers": {"Authorization": f"Bearer {token}"},
        "project_id": project.id,
    }


def test_node_contracts_catalog(auth):
    """The canvas palette fetches the typed node-contract catalog."""
    client, headers = auth["client"], auth["headers"]
    resp = client.get("/api/code/node-contracts", headers=headers)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    catalog = resp.get_json()["data"]["node_contracts"]
    by_type = {c["node_type"]: c for c in catalog}
    assert {"requirements", "flow", "deploy"} <= set(by_type)
    assert by_type["requirements"]["executable"] is True
    assert by_type["requirements"]["outputs"][0]["type"] == "code:requirements_doc"


def test_freeze_canvas_pins_stage_prompts(auth):
    """Freezing stamps a frozen prompt pin onto each typed stage node."""
    client, headers, pid = auth["client"], auth["headers"], auth["project_id"]
    nodes = [
        {
            "id": "R",
            "type": "stage",
            "position": {"x": 0, "y": 0},
            "data": {"label": "需求", "config": {"contract_key": "requirements"}},
        }
    ]
    resp = client.post(
        f"/api/code/projects/{pid}/canvases", json={"name": "c", "nodes": nodes}, headers=headers
    )
    cid = resp.get_json()["data"]["canvas"]["id"]

    resp = client.post(f"/api/code/projects/{pid}/canvases/{cid}/freeze", headers=headers)
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = resp.get_json()["data"]
    assert data["pinned"] == 1
    rnode = next(n for n in data["canvas"]["nodes"] if n["id"] == "R")
    assert rnode["data"]["config"]["prompt_pin"]["key"] == "code/requirements_prompt.txt"


def test_canvas_crud_roundtrip(auth, monkeypatch):
    client, headers, pid = auth["client"], auth["headers"], auth["project_id"]

    # Create (frontend seeds source nodes on first open).
    nodes = [
        {
            "id": "s1",
            "type": "source_doc",
            "position": {"x": 40, "y": 40},
            "data": {"label": "需求", "config": {"source_kind": "requirements_doc"}},
        }
    ]
    resp = client.post(
        f"/api/code/projects/{pid}/canvases",
        json={"name": "默认画布", "nodes": nodes},
        headers=headers,
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    canvas = resp.get_json()["data"]["canvas"]
    cid = canvas["id"]
    assert canvas["nodes"][0]["id"] == "s1"

    # List
    resp = client.get(f"/api/code/projects/{pid}/canvases", headers=headers)
    assert resp.status_code == 200
    assert len(resp.get_json()["data"]["canvases"]) == 1

    # Get full graph
    resp = client.get(f"/api/code/projects/{pid}/canvases/{cid}", headers=headers)
    assert resp.status_code == 200
    assert resp.get_json()["data"]["canvas"]["nodes"][0]["id"] == "s1"

    # Update (debounce-save)
    resp = client.put(
        f"/api/code/projects/{pid}/canvases/{cid}",
        json={"nodes": nodes + [{"id": "a1", "type": "agent", "position": {"x": 300, "y": 40}, "data": {"label": "分析", "config": {"prompt": "p"}}}], "edges": [{"id": "e1", "source": "s1", "target": "a1"}]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    assert len(resp.get_json()["data"]["canvas"]["nodes"]) == 2


def test_run_creation_accepts_canvas_workflow(auth, monkeypatch):
    client, headers, pid = auth["client"], auth["headers"], auth["project_id"]

    # Don't actually spawn the background worker (its thread can't see the
    # in-memory test DB); we only assert the route accepts + queues the run.
    import backend.routes.agent_routes as routes

    monkeypatch.setattr(routes.agent_runtime, "start", lambda *a, **k: None)

    # A canvas to run.
    resp = client.post(
        f"/api/code/projects/{pid}/canvases",
        json={"name": "c", "nodes": []},
        headers=headers,
    )
    cid = resp.get_json()["data"]["canvas"]["id"]

    # runCanvas() — the exact body the frontend sends.
    resp = client.post(
        "/api/agent/runs",
        json={
            "domain": "code",
            "workflow": "code_canvas_generation",
            "resource_type": "code_project",
            "resource_id": pid,
            "config": {"canvas_id": cid},
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    body = resp.get_json()["data"]
    assert body["status"] == "queued"
    assert body["run_id"]


def test_run_creation_rejects_missing_canvas_id(auth):
    client, headers, pid = auth["client"], auth["headers"], auth["project_id"]
    resp = client.post(
        "/api/agent/runs",
        json={
            "domain": "code",
            "workflow": "code_canvas_generation",
            "resource_type": "code_project",
            "resource_id": pid,
            "config": {},
        },
        headers=headers,
    )
    assert resp.status_code == 400
