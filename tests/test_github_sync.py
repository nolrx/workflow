"""
Unit tests for GitHub auto-sync assembly + push (no network, no DB).

Covers the deterministic full-stack file-set layout (`collect_project_files`),
the zip unpack prefixing + oversized-skip reporting, the deploy-validated backend
preference, the Git Data API call sequence of a full-snapshot push
(`push_snapshot`), the idempotent secondary-dev branch fork (`_ensure_dev_branch`),
the deploy-aware commit message, and that `autosync_after_run` is a clean no-op
when the integration is unconfigured.
"""
import io
import zipfile
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


def _patch_collectors(monkeypatch, *, frontend=None, backend=None, middleware=None, contract=None, dist=None):
    """Isolate collect_project_files from the DB by stubbing every component collector."""
    monkeypatch.setattr(sync_service, "_collect_frontend_source", lambda pid, skipped=None: dict(frontend or {}))
    monkeypatch.setattr(sync_service, "_collect_backend_source", lambda pid, skipped=None: dict(backend or {}))
    monkeypatch.setattr(sync_service, "_collect_middleware", lambda pid: dict(middleware or {}))
    monkeypatch.setattr(sync_service, "_collect_contract", lambda pid: dict(contract or {}))
    monkeypatch.setattr(sync_service, "_collect_dist", lambda pid: dict(dist or {}))


# --- naming ------------------------------------------------------------------
def test_repo_name_is_slugged_and_suffixed(monkeypatch):
    monkeypatch.delenv("GITHUB_REPO_PREFIX", raising=False)
    name = sync_service.repo_name_for(_fake_project(title="My Cool App!!", id="abcdef123456"))
    assert name == "my-cool-app-abcdef12"


def test_repo_name_prefix(monkeypatch):
    monkeypatch.setenv("GITHUB_REPO_PREFIX", "studio-")
    name = sync_service.repo_name_for(_fake_project(title="X", id="abcdef123456"))
    assert name.startswith("studio-")


# --- zip unpack: prefixing + oversized reporting -----------------------------
def test_unzip_prefixes_and_reports_oversized(monkeypatch, tmp_path):
    monkeypatch.setattr(sync_service, "upload_root", lambda: tmp_path)
    monkeypatch.setattr(sync_service, "_MAX_FILE_BYTES", 5)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("main.py", b"ok")  # 2 bytes -> kept
        zf.writestr("big.bin", b"x" * 20)  # 20 bytes -> skipped
    (tmp_path / "be.zip").write_bytes(buf.getvalue())

    artifact = SimpleNamespace(id="a1", storage_path="be.zip")
    skipped: list = []
    files = sync_service._unzip_artifact_files(artifact, prefix="backend/", skipped=skipped)

    assert files == {"backend/main.py": b"ok"}
    assert skipped == [{"path": "backend/big.bin", "size": 20}]


def test_unzip_missing_artifact_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(sync_service, "upload_root", lambda: tmp_path)
    assert sync_service._unzip_artifact_files(None, prefix="backend/", skipped=None) == {}
    art = SimpleNamespace(id="a1", storage_path="nope.zip")
    assert sync_service._unzip_artifact_files(art, prefix="backend/", skipped=None) == {}


# --- backend prefers the deploy-validated (repaired) source -------------------
def test_backend_prefers_repaired_source(monkeypatch):
    seen = []

    def fake_latest(pid, drt):
        seen.append(drt)
        return SimpleNamespace(id=drt) if drt == "code_backend_project_repaired_zip" else None

    captured = {}

    def fake_unzip(artifact, *, prefix, skipped=None):
        captured["artifact"] = artifact
        captured["prefix"] = prefix
        return {f"{prefix}main.py": b"x"}

    monkeypatch.setattr(sync_service, "_latest_artifact", fake_latest)
    monkeypatch.setattr(sync_service, "_unzip_artifact_files", fake_unzip)

    out = sync_service._collect_backend_source("p1")
    assert seen[0] == "code_backend_project_repaired_zip"  # repaired queried first
    assert captured["artifact"].id == "code_backend_project_repaired_zip"
    assert captured["prefix"] == "backend/"
    assert "backend/main.py" in out


def test_backend_falls_back_to_generation_source(monkeypatch):
    def fake_latest(pid, drt):
        return SimpleNamespace(id=drt) if drt == "code_backend_project_zip" else None

    captured = {}

    def fake_unzip(artifact, *, prefix, skipped=None):
        captured["artifact"] = artifact
        return {f"{prefix}app.py": b"x"}

    monkeypatch.setattr(sync_service, "_latest_artifact", fake_latest)
    monkeypatch.setattr(sync_service, "_unzip_artifact_files", fake_unzip)

    sync_service._collect_backend_source("p1")
    assert captured["artifact"].id == "code_backend_project_zip"


# --- file collection / layout ------------------------------------------------
def test_collect_docs_and_generated_readme(monkeypatch):
    _patch_collectors(monkeypatch)
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
    # A README + .gitignore are always generated for the monorepo root.
    assert b"My Cool App" in files["README.md"]
    assert "node_modules/" in files[".gitignore"].decode()


def test_collect_fullstack_layout(monkeypatch):
    _patch_collectors(
        monkeypatch,
        frontend={"frontend/package.json": b"{}", "frontend/src/App.tsx": b"x"},
        backend={"backend/Dockerfile": b"FROM node", "backend/main.py": b"y"},
        middleware={"db/init.sql": b"CREATE TABLE t();"},
        contract={"contract/openapi.json": b"{}"},
    )
    files = sync_service.collect_project_files(_fake_project())

    assert files["frontend/package.json"] == b"{}"
    assert files["backend/Dockerfile"] == b"FROM node"
    assert files["db/init.sql"]
    assert files["contract/openapi.json"]
    # README mentions the full-stack layout + the secondary-dev branch.
    readme = files["README.md"].decode()
    assert "frontend/" in readme and "backend/" in readme
    assert sync_service.dev_branch_name() in readme


def test_collect_dist_passthrough(monkeypatch):
    _patch_collectors(
        monkeypatch,
        frontend={"frontend/index.tsx": b"x"},
        dist={"frontend/dist/index.html": b"<html>"},
    )
    files = sync_service.collect_project_files(_fake_project())
    assert files["frontend/dist/index.html"] == b"<html>"


def test_dist_disabled_by_default(monkeypatch):
    monkeypatch.delenv("GITHUB_PUSH_DIST", raising=False)
    assert sync_service._push_dist_enabled() is False
    monkeypatch.setenv("GITHUB_PUSH_DIST", "true")
    assert sync_service._push_dist_enabled() is True


# --- commit message ----------------------------------------------------------
def test_commit_message_non_deploy():
    msg = sync_service._commit_message(_fake_project(), "code_full_generation", "runid12345")
    assert msg.startswith("chore: sync code_full_generation")
    assert "session proj1234" in msg


def test_commit_message_deploy_includes_image_tag(monkeypatch):
    fake_dep = SimpleNamespace(image_tag="app-deadbeef:itest1")

    class _Q:
        def filter_by(self, **kw):
            return self

        def first(self):
            return fake_dep

    monkeypatch.setattr(sync_service, "CodeDeployment", SimpleNamespace(query=_Q()))
    msg = sync_service._commit_message(_fake_project(), "code_fullstack_deploy", "runid12345")
    assert msg.startswith("deploy:")
    assert "app-deadbeef:itest1" in msg


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

    def init_empty_repo(self, owner, repo, branch="main"):
        self.calls.append(("init_empty_repo", branch))
        return "seed-sha"

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


def test_push_snapshot_empty_repo_seeds_then_updates_ref():
    client = _FakeClient(existing_ref=None)
    files = {"README.md": b"hi", "frontend/src/a.ts": b"x"}
    sha = sync_service.push_snapshot(client, _link(), files, "msg")

    assert sha == "commit-sha"
    assert client.blobs == 2
    # Full snapshot: tree built WITHOUT a base_tree.
    create_tree = next(c for c in client.calls if c[0] == "create_tree")
    assert create_tree[2] is None
    # An empty repo (no ref) is seeded via the Contents API first; the snapshot
    # commit is then parented on that seed and the existing ref is fast-forwarded
    # (NOT create_ref — the seed already created heads/main).
    assert ("init_empty_repo", "main") in client.calls
    commit = next(c for c in client.calls if c[0] == "create_commit")
    assert commit[2] == ["seed-sha"]
    assert any(c[0] == "update_ref" and c[1] == "heads/main" for c in client.calls)
    assert not any(c[0] == "create_ref" for c in client.calls)


def test_push_snapshot_existing_repo_updates_ref():
    client = _FakeClient(existing_ref={"object": {"sha": "base-sha"}})
    sha = sync_service.push_snapshot(client, _link(), {"README.md": b"hi"}, "msg")

    assert sha == "commit-sha"
    commit = next(c for c in client.calls if c[0] == "create_commit")
    assert commit[2] == ["base-sha"]  # parented on the existing head
    assert any(c[0] == "update_ref" and c[1] == "heads/main" for c in client.calls)
    assert not any(c[0] == "create_ref" for c in client.calls)


def test_push_snapshot_custom_branch():
    client = _FakeClient(existing_ref=None)
    sync_service.push_snapshot(client, _link(), {"a": b"x"}, "msg", branch="release")
    assert any(c == ("get_ref", "heads/release") for c in client.calls)
    # Empty repo: seeded on the requested branch, then fast-forwarded.
    assert ("init_empty_repo", "release") in client.calls
    assert any(c[0] == "update_ref" and c[1] == "heads/release" for c in client.calls)


# --- secondary-dev branch ----------------------------------------------------
def test_ensure_dev_branch_creates_when_absent(monkeypatch):
    monkeypatch.delenv("GITHUB_DEV_BRANCH", raising=False)
    client = _FakeClient(existing_ref=None)
    branch = sync_service._ensure_dev_branch(client, _link(), "sha123")
    assert branch == "dev"
    assert any(
        c[0] == "create_ref" and c[1] == "refs/heads/dev" and c[2] == "sha123" for c in client.calls
    )


def test_ensure_dev_branch_idempotent_never_overwrites(monkeypatch):
    monkeypatch.delenv("GITHUB_DEV_BRANCH", raising=False)
    client = _FakeClient(existing_ref={"object": {"sha": "old"}})
    branch = sync_service._ensure_dev_branch(client, _link(), "sha123")
    assert branch == "dev"
    # Already exists -> we must NOT create/overwrite it (preserve user edits).
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
