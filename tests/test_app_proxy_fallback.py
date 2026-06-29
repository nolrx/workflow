"""
Route-level tests for the deployed-app reverse proxy (`/app/<pid>/api/<sub>` →
container) fallback in ``fullstack_routes.proxy_to_backend``.

The proxy strips ``/app/<pid>/api`` and forwards ``/<sub>`` to the container root.
A generated backend that mounted its routes UNDER an ``/api`` prefix (contra the
root-mount contract — e.g. FastAPI ``include_router(prefix="/api/v1")``) therefore
404s every call the served frontend makes. The proxy must retry ONCE with the
``/api/`` prefix restored so such an app works end-to-end, while a conformant
root-mounted app pays for no extra request.
"""
import pytest

from backend.app import create_app
from backend.extensions import db


class _FakeResp:
    def __init__(self, status_code, body=b'{"ok":true}'):
        self.status_code = status_code
        self.headers = {"Content-Type": "application/json"}
        self._body = body
        self.closed = False

    def iter_content(self, chunk_size=8192):
        yield self._body

    def close(self):
        self.closed = True


@pytest.fixture
def ctx(monkeypatch):
    application = create_app("testing")
    with application.app_context():
        db.create_all()
        from backend.models.code import CodeProject, CodeProjectStatus
        from backend.services.code import deploy_service

        # Public project → the proxy authorizes anonymously (no cookie needed).
        project = CodeProject(
            user_id="u1", title="T", requirement_input="r", requirements_doc="#",
            style_prompt="s", status=CodeProjectStatus.UI_CONFIRMED, visibility="public",
        )
        db.session.add(project)
        db.session.commit()
        monkeypatch.setattr(deploy_service, "resolve_proxy_target",
                            lambda pid, uid: ("app-x", 8080))
        yield {"client": application.test_client(), "pid": project.id}
        db.session.remove()
        db.drop_all()


def test_retries_with_api_prefix_on_404(ctx, monkeypatch):
    from backend.routes.code import fullstack_routes

    calls = []

    def fake_request(method, url, **kw):
        calls.append(url)
        # The stripped path 404s; the ``/api/``-restored retry resolves.
        return _FakeResp(404 if "/api/" not in url else 200)

    monkeypatch.setattr(fullstack_routes.requests, "request", fake_request)

    resp = ctx["client"].post(f"/app/{ctx['pid']}/api/v1/auth/register", json={"x": 1})
    assert resp.status_code == 200
    assert calls == [
        "http://app-x:8080/v1/auth/register",
        "http://app-x:8080/api/v1/auth/register",
    ]


def test_no_retry_when_root_mounted(ctx, monkeypatch):
    from backend.routes.code import fullstack_routes

    calls = []

    def fake_request(method, url, **kw):
        calls.append(url)
        return _FakeResp(200)

    monkeypatch.setattr(fullstack_routes.requests, "request", fake_request)

    resp = ctx["client"].get(f"/app/{ctx['pid']}/api/health")
    assert resp.status_code == 200
    assert calls == ["http://app-x:8080/health"]  # conformant app: single request


def test_genuine_404_not_masked_and_retry_bounded(ctx, monkeypatch):
    from backend.routes.code import fullstack_routes

    calls = []

    def fake_request(method, url, **kw):
        calls.append(url)
        return _FakeResp(404)  # neither path exists → real 404 surfaces

    monkeypatch.setattr(fullstack_routes.requests, "request", fake_request)

    resp = ctx["client"].get(f"/app/{ctx['pid']}/api/v1/missing")
    assert resp.status_code == 404
    # Exactly one retry — never an unbounded loop.
    assert calls == [
        "http://app-x:8080/v1/missing",
        "http://app-x:8080/api/v1/missing",
    ]


def test_no_double_prefix_when_subpath_already_api(ctx, monkeypatch):
    from backend.routes.code import fullstack_routes

    calls = []

    def fake_request(method, url, **kw):
        calls.append(url)
        return _FakeResp(404)

    monkeypatch.setattr(fullstack_routes.requests, "request", fake_request)

    # A call already addressed to ``/api/...`` must not be retried as ``/api/api/...``.
    resp = ctx["client"].get(f"/app/{ctx['pid']}/api/api/health")
    assert resp.status_code == 404
    assert calls == ["http://app-x:8080/api/health"]
