"""
Unit tests for GitHub auto-sync assembly + push (no network, no DB).

Covers the deterministic file-set layout (`collect_project_files`), the Git Data
API call sequence of a full-snapshot push (`push_snapshot`), and that
`autosync_after_run` is a clean no-op when the integration is unconfigured.
"""
from types import SimpleNamespace

from backend.services.code.github import sync_service


def _fake_project(**overrides):
    docs = overrides.pop("documents", [])
    defaults = dict(
        id="proj1234abcd",
        title="My Cool App",
        user_id="u1",
        team_id=None,
        requirement_input="Build a todo app",
        requirements_doc="# Requirements\n...",
        development_flow="# Flow\n...",
        style_prompt="# Style\n...",
        ui_baseline_prompt=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(documents=SimpleNamespace(all=lambda: docs), **defaults)


# --- naming ------------------------------------------------------------------
def test_repo_name_is_slugged_and_suffixed(monkeypatch):
    monkeypatch.delenv("GITHUB_REPO_PREFIX", raising=False)
    name = sync_service.repo_name_for(_fake_project(title="My Cool App!!", id="abcdef123456"))
    assert name == "my-cool-app-abcdef12"


def test_repo_name_prefix(monkeypatch):
    monkeypatch.setenv("GITHUB_REPO_PREFIX", "studio-")
    name = sync_service.repo_name_for(_fake_project(title="X", id="abcdef123456"))
    assert name.startswith("studio-")


# --- file collection ---------------------------------------------------------
def test_collect_docs_and_generated_readme(monkeypatch):
    monkeypatch.setattr(sync_service, "_collect_frontend_source", lambda pid: {})
    monkeypatch.setattr(sync_service, "_collect_dist", lambda pid: {})
    docs = [
        SimpleNamespace(order_index=0, title="API 设计", content="api doc"),
        SimpleNamespace(order_index=1, title="Data Model", content="data doc"),
    ]
    files = sync_service.collect_project_files(_fake_project(documents=docs))

    assert files["docs/requirements.md"] == b"# Requirements\n..."
    assert files["docs/development-flow.md"]
    assert files["docs/style.md"]
    assert "docs/ui-baseline.md" not in files  # None field omitted
    assert any(p.startswith("docs/00-") for p in files)
    assert any(p.startswith("docs/01-data-model") for p in files)
    # No source -> a README is generated.
    assert b"My Cool App" in files["README.md"]


def test_collect_keeps_project_own_readme(monkeypatch):
    source = {"package.json": b"{}", "src/App.tsx": b"x", "README.md": b"project readme"}
    monkeypatch.setattr(sync_service, "_collect_frontend_source", lambda pid: dict(source))
    monkeypatch.setattr(sync_service, "_collect_dist", lambda pid: {})
    files = sync_service.collect_project_files(_fake_project())

    assert files["package.json"] == b"{}"
    assert files["src/App.tsx"] == b"x"
    # The project's own README wins over a generated one.
    assert files["README.md"] == b"project readme"


def test_collect_includes_dist(monkeypatch):
    monkeypatch.setattr(sync_service, "_collect_frontend_source", lambda pid: {"index.tsx": b"x"})
    monkeypatch.setattr(
        sync_service, "_collect_dist", lambda pid: {"dist/index.html": b"<html>"}
    )
    files = sync_service.collect_project_files(_fake_project())
    assert files["dist/index.html"] == b"<html>"


# --- push snapshot -----------------------------------------------------------
class _FakeClient:
    """Records the Git Data API calls a push makes."""

    def __init__(self, existing_ref=None):
        self._existing = existing_ref
        self.calls = []
        self.blobs = 0

    def get_ref(self, owner, repo, ref):
        self.calls.append(("get_ref", ref))
        return self._existing

    def create_blob(self, owner, repo, content):
        self.blobs += 1
        return f"blob{self.blobs}"

    def create_tree(self, owner, repo, tree, base_tree=None):
        self.calls.append(("create_tree", len(tree), base_tree))
        return {"sha": "tree-sha"}

    def create_commit(self, owner, repo, message, tree_sha, parents):
        self.calls.append(("create_commit", tree_sha, list(parents)))
        return {"sha": "commit-sha"}

    def update_ref(self, owner, repo, ref, sha, *, force=False):
        self.calls.append(("update_ref", ref, sha, force))
        return {}

    def create_ref(self, owner, repo, ref, sha):
        self.calls.append(("create_ref", ref, sha))
        return {}


def _link():
    return SimpleNamespace(repo_owner="acme", repo_name="app", default_branch="main")


def test_push_snapshot_empty_repo_creates_ref():
    client = _FakeClient(existing_ref=None)
    files = {"README.md": b"hi", "src/a.ts": b"x"}
    sha = sync_service.push_snapshot(client, _link(), files, "msg")

    assert sha == "commit-sha"
    assert client.blobs == 2
    # Full snapshot: tree built WITHOUT a base_tree.
    create_tree = next(c for c in client.calls if c[0] == "create_tree")
    assert create_tree[2] is None
    # First commit has no parents; ref is created (not updated).
    commit = next(c for c in client.calls if c[0] == "create_commit")
    assert commit[2] == []
    assert any(c[0] == "create_ref" and c[1] == "refs/heads/main" for c in client.calls)
    assert not any(c[0] == "update_ref" for c in client.calls)


def test_push_snapshot_existing_repo_updates_ref():
    client = _FakeClient(existing_ref={"object": {"sha": "base-sha"}})
    sha = sync_service.push_snapshot(client, _link(), {"README.md": b"hi"}, "msg")

    assert sha == "commit-sha"
    commit = next(c for c in client.calls if c[0] == "create_commit")
    assert commit[2] == ["base-sha"]  # parented on the existing head
    assert any(c[0] == "update_ref" and c[1] == "heads/main" for c in client.calls)
    assert not any(c[0] == "create_ref" for c in client.calls)


# --- entry point -------------------------------------------------------------
def test_autosync_noop_when_unconfigured(monkeypatch):
    monkeypatch.setattr(sync_service.app_auth, "is_configured", lambda: False)
    emitted = []
    recorder = SimpleNamespace(emit=lambda *a, **k: emitted.append((a, k)))
    run = SimpleNamespace(id="run1", resource_id="proj1", workflow="code_full_generation")

    # Returns cleanly without emitting any event or touching the DB.
    assert sync_service.autosync_after_run(recorder, run) is None
    assert emitted == []
