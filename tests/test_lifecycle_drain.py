"""
Unit tests for graceful-drain lifecycle (single-machine redeploy support).

Network-free. Covers: the process drain flag + guard, the liveness/readiness
split, that an endpoint which STARTS new work returns 503 while draining, and the
token-guarded ops endpoints that the deploy script drives.
"""
import pytest
from flask_jwt_extended import create_access_token

from backend.app import create_app
from backend.extensions import db
from backend.services import lifecycle


@pytest.fixture
def app():
    application = create_app("testing")
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def _reset_drain():
    """Drain state is a process global — reset around every test."""
    lifecycle.end_drain()
    yield
    lifecycle.end_drain()


# --- primitives --------------------------------------------------------------
def test_drain_flag_roundtrip():
    assert lifecycle.is_draining() is False
    lifecycle.begin_drain()
    assert lifecycle.is_draining() is True
    lifecycle.end_drain()
    assert lifecycle.is_draining() is False


def test_drain_guard_only_blocks_while_draining(app):
    with app.app_context():
        assert lifecycle.drain_guard() is None
        lifecycle.begin_drain()
        resp = lifecycle.drain_guard()
        assert resp is not None
        body, status = resp
        assert status == 503
        assert body.get_json()["error"] == "DRAINING"


# --- health: liveness vs readiness -------------------------------------------
def test_liveness_is_always_up_even_while_draining(client):
    assert client.get("/health").status_code == 200
    lifecycle.begin_drain()
    assert client.get("/health").status_code == 200  # process is still alive


def test_readiness_flips_to_503_while_draining(client):
    assert client.get("/health/ready").status_code == 200
    lifecycle.begin_drain()
    r = client.get("/health/ready")
    assert r.status_code == 503
    assert r.get_json()["ready"] is False


# --- a start-new-work endpoint refuses while draining ------------------------
def test_create_run_refused_while_draining(app, client):
    with app.app_context():
        token = create_access_token(identity="u1")
    headers = {"Authorization": f"Bearer {token}"}

    # Not draining: the guard passes, so we fall through to normal validation
    # (empty body → VALIDATION_ERROR), proving the guard isn't the blocker.
    assert client.post("/api/agent/runs", json={}, headers=headers).status_code == 400

    lifecycle.begin_drain()
    r = client.post("/api/agent/runs", json={}, headers=headers)
    assert r.status_code == 503
    assert r.get_json()["error"] == "DRAINING"


# --- token-guarded ops endpoints ---------------------------------------------
def test_lifecycle_endpoints_disabled_without_token(client, monkeypatch):
    monkeypatch.delenv("DEPLOY_CONTROL_TOKEN", raising=False)
    assert client.post("/api/admin/lifecycle/drain").status_code == 403


def test_lifecycle_endpoints_reject_bad_token(client, monkeypatch):
    monkeypatch.setenv("DEPLOY_CONTROL_TOKEN", "s3cret")
    assert client.post("/api/admin/lifecycle/drain").status_code == 403  # missing header
    bad = client.post("/api/admin/lifecycle/drain", headers={"X-Deploy-Token": "nope"})
    assert bad.status_code == 403
    assert lifecycle.is_draining() is False  # rejected calls never flipped the flag


def test_lifecycle_drain_undrain_with_token(client, monkeypatch):
    monkeypatch.setenv("DEPLOY_CONTROL_TOKEN", "s3cret")
    hdr = {"X-Deploy-Token": "s3cret"}

    assert client.post("/api/admin/lifecycle/drain", headers=hdr).status_code == 200
    assert lifecycle.is_draining() is True
    status = client.get("/api/admin/lifecycle/status", headers=hdr)
    assert status.get_json()["data"]["draining"] is True
    # Readiness reflects it while ops drives drain directly.
    assert client.get("/health/ready").status_code == 503

    assert client.post("/api/admin/lifecycle/undrain", headers=hdr).status_code == 200
    assert lifecycle.is_draining() is False
    assert client.get("/health/ready").status_code == 200
