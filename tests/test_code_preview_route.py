"""
Route-level tests for the session-bound deployed preview (`/preview/<project_id>`).

Exercises the auth + resolution contract of ``code/preview_routes.py``: ownership
proven by a one-shot ``?token=`` JWT that gets pinned into a path-scoped cookie via
a redirect to a token-less URL, and the URL resolving to the project's latest
*built* frontend run's on-disk dist.
"""
import pytest

from backend.app import create_app
from backend.extensions import db


@pytest.fixture
def ctx(tmp_path):
    application = create_app("testing")
    application.config["UPLOAD_FOLDER"] = str(tmp_path / "uploads")
    with application.app_context():
        db.create_all()

        from flask_jwt_extended import create_access_token

        from backend.models.agent.run import AgentRun, AgentRunStatus
        from backend.models.code import CodeProject, CodeProjectStatus

        user_id = "u-prev"
        project = CodeProject(
            user_id=user_id,
            title="T",
            requirement_input="r",
            requirements_doc="# 需求",
            style_prompt="s",
            status=CodeProjectStatus.UI_CONFIRMED,
        )
        db.session.add(project)
        db.session.commit()

        run = AgentRun(
            user_id=user_id,
            domain="code",
            workflow="code_frontend_project_generation",
            resource_type="code_project",
            resource_id=project.id,
            status=AgentRunStatus.COMPLETED,
        )
        db.session.add(run)
        db.session.commit()

        # Lay down a built dist for that run.
        site_dir = tmp_path / "uploads" / "agent_runs" / run.id / "site"
        (site_dir / "assets").mkdir(parents=True)
        (site_dir / "index.html").write_text(
            "<!doctype html><script src='./assets/app.js'></script>", encoding="utf-8"
        )
        (site_dir / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")

        token = create_access_token(identity=user_id)
        other_token = create_access_token(identity="someone-else")

        yield {
            "client": application.test_client(),
            "pid": project.id,
            "token": token,
            "other_token": other_token,
        }
        db.session.remove()
        db.drop_all()


def test_rejects_missing_or_invalid_token(ctx):
    client, pid = ctx["client"], ctx["pid"]
    assert client.get(f"/preview/{pid}/").status_code == 403
    assert client.get(f"/preview/{pid}/?token=garbage").status_code == 403


def test_other_user_cannot_preview(ctx):
    # A non-owner with a valid token, against a PRIVATE project: the route now
    # supports public sharing (visibility == 'public' serves anonymously), so a
    # non-owner of a non-public project is denied with 403 FORBIDDEN.
    resp = ctx["client"].get(f"/preview/{ctx['pid']}/?token={ctx['other_token']}")
    assert resp.status_code == 403


def test_404_when_no_built_run(ctx, tmp_path):
    # A fresh project (no frontend run) has nothing to preview.
    from backend.models.code import CodeProject, CodeProjectStatus

    bare = CodeProject(
        user_id="u-prev", title="T2", requirement_input="r",
        requirements_doc="# 需求", style_prompt="s", status=CodeProjectStatus.UI_CONFIRMED,
    )
    db.session.add(bare)
    db.session.commit()
    resp = ctx["client"].get(f"/preview/{bare.id}/?token={ctx['token']}")
    assert resp.status_code == 404


def test_entry_redirects_and_pins_cookie(ctx):
    client, pid, token = ctx["client"], ctx["pid"], ctx["token"]
    resp = client.get(f"/preview/{pid}/?token={token}")
    # Entry request: redirect to a token-less URL with a path-scoped cookie.
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(f"/preview/{pid}/")
    # Werkzeug emits one Set-Cookie header per cookie; join them to assert over all.
    set_cookies = "\n".join(resp.headers.getlist("Set-Cookie"))
    assert "fe_preview_token=" in set_cookies
    assert f"Path=/preview/{pid}/" in set_cookies
    # The app-token cookie is planted on EVERY owner entry — even with NO running
    # deployment — so a later deploy + token-less reload can still reach
    # /app/<pid>/api without a 403. Scoped to /app/<pid>/ so it rides only those calls.
    assert "fs_app_token=" in set_cookies
    assert f"Path=/app/{pid}/" in set_cookies
    # Both cookies hold a minted, longer-lived preview token (NOT the 30-min access
    # token) so a left-open preview tab keeps working — Max-Age matches the TTL.
    from backend.utils.preview_token import PREVIEW_TOKEN_TTL

    assert f"Max-Age={PREVIEW_TOKEN_TTL}" in set_cookies
    assert PREVIEW_TOKEN_TTL > 1800
    # The pinned cookie value is a freshly minted token, not the entry access token.
    assert f"fe_preview_token={token}" not in set_cookies


def test_minted_cookie_outlives_access_token_and_is_project_scoped(ctx):
    # A minted preview token authenticates the token-less follow-up requests and is
    # pinned to its project: replaying it under a different project's path is rejected.
    from backend.routes.code.preview_routes import _PREVIEW_COOKIE
    from backend.utils.preview_token import mint_preview_token, preview_identity

    client, pid, token = ctx["client"], ctx["pid"], ctx["token"]
    resp = client.get(f"/preview/{pid}/?token={token}")
    set_cookies = "\n".join(resp.headers.getlist("Set-Cookie"))
    # Pull the minted value out of the Set-Cookie header and verify its scope.
    minted = set_cookies.split(f"{_PREVIEW_COOKIE}=", 1)[1].split(";", 1)[0]
    assert preview_identity(minted, f"project:{pid}") == "u-prev"
    assert preview_identity(minted, "project:some-other-pid") is None
    # A token minted for another project cannot authenticate THIS project's preview.
    foreign = mint_preview_token("u-prev", "project:other")
    assert client.get(f"/preview/{pid}/?token={foreign}").status_code == 403


def test_serves_index_and_assets_via_cookie(ctx):
    client, pid, token = ctx["client"], ctx["pid"], ctx["token"]
    # Follow the entry redirect — the cookie jar carries the pinned token onward.
    resp = client.get(f"/preview/{pid}/?token={token}", follow_redirects=True)
    assert resp.status_code == 200
    assert b"doctype html" in resp.data
    assert resp.headers.get("Content-Security-Policy", "").startswith("default-src 'self'")
    # Relative asset request (no query token) authenticates via the cookie.
    asset = client.get(f"/preview/{pid}/assets/app.js")
    assert asset.status_code == 200
    assert b"console.log(1)" in asset.data
