"""
Route-level tests for token refresh (`auth_routes.refresh`).

Regression guard for the production incident where every `POST /api/auth/refresh`
returned 401: the web client posts the refresh token in the JSON body
(`{"refresh_token": ...}`) with no Authorization header, but the endpoint's
`@jwt_required(refresh=True)` only looked in the Authorization header. The fix
is `JWT_TOKEN_LOCATION = ["headers", "json"]` in config, so the refresh token is
accepted from either transport. These tests assert BOTH paths mint a fresh access
token, and that a missing/garbage token is still rejected.
"""
import pytest

from backend.app import create_app
from backend.extensions import db


@pytest.fixture
def ctx():
    application = create_app("testing")
    with application.app_context():
        db.create_all()

        from flask_jwt_extended import create_refresh_token

        user_id = "u-refresh"
        refresh_token = create_refresh_token(identity=user_id)
        client = application.test_client()
        yield client, refresh_token
        db.session.remove()
        db.drop_all()


def test_refresh_with_token_in_json_body(ctx):
    """The web client's transport: token in the JSON body, no auth header."""
    client, refresh_token = ctx
    res = client.post("/api/auth/refresh", json={"refresh_token": refresh_token})
    assert res.status_code == 200, res.get_data(as_text=True)
    assert res.get_json().get("access_token")


def test_refresh_with_token_in_authorization_header(ctx):
    """The flask-jwt-extended default transport: token in the Authorization header."""
    client, refresh_token = ctx
    res = client.post(
        "/api/auth/refresh",
        headers={"Authorization": f"Bearer {refresh_token}"},
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    assert res.get_json().get("access_token")


def test_refresh_without_any_token_is_rejected(ctx):
    """No token in either transport -> 401 (no silent auth bypass)."""
    client, _ = ctx
    res = client.post("/api/auth/refresh", json={})
    assert res.status_code == 401


def test_refresh_rejects_access_token(ctx):
    """An ACCESS token must not be usable on the refresh endpoint."""
    client, _ = ctx
    from flask_jwt_extended import create_access_token

    access_token = create_access_token(identity="u-refresh")
    res = client.post("/api/auth/refresh", json={"refresh_token": access_token})
    assert res.status_code == 422
