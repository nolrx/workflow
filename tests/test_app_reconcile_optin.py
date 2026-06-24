"""
Guard: `create_app()` must be side-effect-free; only `serve()` reconciles on boot.

Regression for a self-inflicted incident: an ad-hoc `create_app('production')`
(run just to mint a token) triggered `reconcile_orphaned_runs`, which re-dispatched
EVERY in-flight run platform-wide and spawned a burst of duplicate sandbox
containers. Reconciliation now runs only on the real server entrypoint, so no
script / test / ops one-liner that merely builds an app can storm production.
"""
import pytest


@pytest.fixture
def spy_reconcile(monkeypatch):
    calls = []
    # app.py imports the name locally at call time, so patching the module
    # attribute is what the local `from ... import reconcile_orphaned_runs` sees.
    monkeypatch.setattr(
        "backend.services.agent.runtime.reconcile_orphaned_runs",
        lambda app: calls.append(app),
    )
    return calls


def test_create_app_does_not_reconcile(spy_reconcile):
    from backend.app import create_app

    create_app("testing")
    assert spy_reconcile == [], "plain create_app() must not reconcile orphaned runs"


def test_create_app_explicit_flag_reconciles(spy_reconcile):
    from backend.app import create_app

    create_app("testing", reconcile_on_boot=True)
    assert len(spy_reconcile) == 1, "reconcile must run when explicitly opted in"


def test_serve_reconciles_once(monkeypatch, spy_reconcile):
    # serve() resolves config from FLASK_ENV; pin it to testing (in-memory db).
    monkeypatch.setenv("FLASK_ENV", "testing")
    from backend.app import serve

    serve()
    assert len(spy_reconcile) == 1, "the real server entrypoint reconciles exactly once"
