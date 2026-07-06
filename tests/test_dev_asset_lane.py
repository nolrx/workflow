"""
Unit tests for the shared asset lane (P2) — network/docker-free.

Covers: the shared bootstrap (skill/gen-assets/genimage content, parameterized
state paths — no /out hardcoding in the Dev variant), output normalization/path
safety, container-side output verification with a faked docker exec (ok /
missing-retryable / env-dead-blocking), the one-shot container script's
diagnostics contract (no regression after extraction), and the dev-turn fold
helper that overrules the reviewer on asset ACs.
"""
import subprocess
import uuid
from types import SimpleNamespace

import pytest

from backend.app import create_app
from backend.extensions import db
from backend.models.code import CodeProject
from backend.models.code.fullstack import (
    CodeDevSession,
    CodeDevTask,
    DevSessionStatus,
    DevTaskStatus,
)
from backend.services.code import asset_lane
from backend.services.code import dev_service as dev_service_mod
from backend.services.code.dev_service import DevService


@pytest.fixture
def app(tmp_path):
    application = create_app("testing")
    application.config["UPLOAD_FOLDER"] = str(tmp_path / "uploads")
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


# --- render_bootstrap -----------------------------------------------------------
def test_bootstrap_contains_full_toolchain():
    script = asset_lane.render_bootstrap(
        style_context_path="/tmp/dev-assets/style_context.txt",
        diagnostics_dir="/tmp/dev-assets",
        style_extract="raw",
    )
    for token in (
        'cat > "$HOME/.fe-assets/genimage.mjs"',
        'cat > "$HOME/bin/gen-assets"',
        "image-assets/SKILL.md",
        "codex login --with-api-key",
        "command -v codex",
        "command -v gen-assets",
        "images/generations",  # genimage.mjs OpenAI endpoint
        "FE_CODEX_TIMEOUT", "FE_GENIMAGE_TIMEOUT",
    ):
        assert token in script, token
    # Heredocs balanced.
    for delim in ("GENIMG_EOF", "GENASSETS_EOF", "SKILL_EOF"):
        assert script.count(delim) == 2


def test_bootstrap_dev_variant_not_hardcoded_to_out():
    script = asset_lane.render_bootstrap(
        style_context_path="/tmp/dev-assets/style_context.txt",
        diagnostics_dir="/tmp/dev-assets",
        style_extract="raw",
    )
    assert "/out/prompt.txt" not in script
    assert "/out/asset_gen.log" not in script
    assert "/tmp/dev-assets/asset_gen.log" in script
    assert "ASSET_STYLE_CONTEXT_FILE:-/tmp/dev-assets/style_context.txt" in script
    assert "head -c 6000" in script  # raw read, no prompt-section awk slicing
    assert "awk '/^# 视觉风格规格/" not in script
    # No leftover template markers.
    assert "__DIAG_DIR__" not in script and "__STYLE_READ__" not in script


def test_bootstrap_oneshot_variant_preserves_out_contract():
    script = asset_lane.render_bootstrap(
        style_context_path="/out/prompt.txt",
        diagnostics_dir="/out",
        style_extract="prompt_sections",
    )
    assert 'echo "invoked: $*" >> /out/asset_gen.log' in script
    assert "command -v codex     > /out/codex_path" in script
    assert "command -v gen-assets > /out/gen_assets_path" in script
    assert "> /out/codex_login.log" in script
    assert "awk '/^# 视觉风格规格/{f=1} /^# 共享 API 契约/{f=0} f'" in script


def test_oneshot_container_script_no_regression():
    """The extracted shared module must keep the one-shot fe-agent script's
    asset-lane diagnostics contract byte-compatible (host readers unchanged)."""
    from backend.services.code.frontend_project_service import _CONTAINER_SCRIPT as s

    for token in (
        "/out/codex_path", "/out/gen_assets_path", "/out/asset_gen.log",
        "/out/codex_login.log", 'cat > "$HOME/bin/gen-assets"',
        "image-assets/SKILL.md", 'export CODEX_HOME="$HOME/.codex"',
        'export PATH="$HOME/bin:$PATH"', "no-codex", "no-key",
    ):
        assert token in s, token
    assert "__ASSET_LANE_BOOTSTRAP__" not in s


def test_dev_entrypoint_injects_asset_lane():
    from backend.services.code.dev_service import _DEV_ENTRYPOINT as s

    assert 'cat > "$HOME/bin/gen-assets"' in s
    assert "/tmp/dev-assets" in s
    assert "__ASSET_LANE_BOOTSTRAP__" not in s


# --- output normalization / path safety --------------------------------------------
def test_safe_asset_path_rules():
    ok, _ = asset_lane.safe_asset_path("src/assets/hero.png")
    assert ok == "src/assets/hero.png"
    ok, _ = asset_lane.safe_asset_path("./src/assets/a.webp")
    assert ok == "src/assets/a.webp"
    for bad in (
        "/etc/passwd.png", "../escape.png", "src/assets/../../etc/x.png",
        "public/logo.png", "https://cdn.example.com/x.png", "src/assets/",
        "src/assets/note.txt", "", None,
    ):
        norm, err = asset_lane.safe_asset_path(bad)
        assert norm is None, bad
        assert err


def test_normalize_outputs_drops_invalid_and_dedupes():
    outputs, warnings = asset_lane.normalize_outputs({
        "outputs": [
            {"path": "src/assets/hero.png", "size": "1536x1024", "prompt": "p", "required": True},
            {"path": "src/assets/hero.png"},  # dup
            {"path": "/abs.png"},
            {"path": "src/assets/b.png", "size": "999x999"},  # bad size -> default
            {"path": "src/assets/c.png", "required": False},
            "not-a-dict",
        ]
    })
    paths = [o["path"] for o in outputs]
    assert paths == ["src/assets/hero.png", "src/assets/b.png", "src/assets/c.png"]
    assert outputs[0]["required"] is True and outputs[2]["required"] is False
    assert outputs[1]["size"] in ("1024x1024", "1536x1024", "1024x1536")
    assert any("重复" in w for w in warnings)
    assert any("绝对路径" in w for w in warnings)
    assert any("尺寸" in w for w in warnings)


def test_normalize_outputs_empty_spec():
    assert asset_lane.normalize_outputs(None) == ([], [])
    assert asset_lane.normalize_outputs({}) == ([], [])


# --- validate_resource_outputs (faked docker) ----------------------------------------
def _fake_run_factory(sizes: dict[str, int], diag: dict):
    """A fake subprocess.run serving both the size probe and the diagnostics read."""

    def fake_run(cmd, **kwargs):
        script = cmd[-1]
        if "codex_path" in script:  # asset_diagnostics
            out = (
                f"codex={'/usr/bin/codex' if diag.get('codex') else ''}\n"
                f"gen={'/home/node/bin/gen-assets' if diag.get('gen') else ''}\n"
                f"key={'1' if diag.get('key') else ''}\n"
                "__LOG__\n" + diag.get("log", "") + "\n__LOGIN__\n"
            )
            return SimpleNamespace(returncode=0, stdout=out, stderr="")
        lines = []
        for path, size in sizes.items():
            lines.append(f"{path}|{size}")
        return SimpleNamespace(returncode=0, stdout="\n".join(lines) + "\n", stderr="")

    return fake_run


_SPEC = {"outputs": [
    {"path": "src/assets/hero.png", "required": True},
    {"path": "src/assets/optional.png", "required": False},
]}


def test_validate_outputs_all_present(monkeypatch):
    monkeypatch.setattr(
        dev_service_mod.subprocess, "run",
        _fake_run_factory({"src/assets/hero.png": 183024, "src/assets/optional.png": 999},
                          {"codex": True, "gen": True, "key": True}),
    )
    res = DevService().validate_resource_outputs("pid", _SPEC)
    assert res["ok"] is True and res["blocking"] is False
    assert res["outputs"][0]["exists"] and res["outputs"][0]["bytes"] == 183024


def test_validate_outputs_missing_required_is_retryable(monkeypatch):
    monkeypatch.setattr(
        dev_service_mod.subprocess, "run",
        _fake_run_factory({"src/assets/hero.png": 0, "src/assets/optional.png": 0},
                          {"codex": True, "gen": True, "key": True, "log": "invoked: x"}),
    )
    res = DevService().validate_resource_outputs("pid", _SPEC)
    assert res["ok"] is False
    assert res["blocking"] is False  # env fine -> retry budget applies
    assert "必需资源缺失" in res["reason"]
    assert "src/assets/hero.png" in res["reason"]


def test_validate_outputs_env_dead_is_blocking(monkeypatch):
    monkeypatch.setattr(
        dev_service_mod.subprocess, "run",
        _fake_run_factory({"src/assets/hero.png": 0, "src/assets/optional.png": 0},
                          {"codex": False, "gen": True, "key": False}),
    )
    res = DevService().validate_resource_outputs("pid", _SPEC)
    assert res["ok"] is False
    assert res["blocking"] is True
    assert "Codex" in res["reason"] and "OPENAI_API_KEY" in res["reason"]
    assert res["diagnostics"]["calls"] == 0


def test_validate_outputs_all_paths_illegal_is_blocking(monkeypatch):
    def boom(*a, **k):  # docker must never be touched for illegal paths
        raise AssertionError("docker exec should not run")

    monkeypatch.setattr(dev_service_mod.subprocess, "run", boom)
    res = DevService().validate_resource_outputs(
        "pid", {"outputs": [{"path": "../../etc/x.png"}]}
    )
    assert res["ok"] is False and res["blocking"] is True
    assert "非法" in res["reason"]


def test_validate_outputs_no_outputs_is_ok(monkeypatch):
    monkeypatch.setattr(
        dev_service_mod.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no docker expected")),
    )
    res = DevService().validate_resource_outputs("pid", {})
    assert res["ok"] is True and res["blocking"] is False


def test_missing_optional_only_is_ok(monkeypatch):
    monkeypatch.setattr(
        dev_service_mod.subprocess, "run",
        _fake_run_factory({"src/assets/hero.png": 5000, "src/assets/optional.png": 0},
                          {"codex": True, "gen": True, "key": True}),
    )
    res = DevService().validate_resource_outputs("pid", _SPEC)
    assert res["ok"] is True and res["blocking"] is False
    assert [r for r in res["outputs"] if not r["exists"]][0]["required"] is False


# --- dev-turn fold: deterministic overrule of the reviewer ---------------------------
def _asset_task(session) -> CodeDevTask:
    t = CodeDevTask(
        project_id=session.project_id, session_id=session.id, feature_id="ASSET.FR1.1",
        category="asset", lane="asset", title="生成主视觉", status=DevTaskStatus.VERIFYING,
    )
    t.set_acceptance_criteria(["src/assets/hero.png 存在且非 0 字节", "不使用远程图片 URL"])
    t.set_resource_spec({"outputs": [{"path": "src/assets/hero.png", "required": True}]})
    db.session.add(t)
    db.session.commit()
    return t


def _mk_session(app):
    project = CodeProject(user_id=str(uuid.uuid4()), title="T", requirement_input="r")
    db.session.add(project)
    db.session.commit()
    s = CodeDevSession(
        project_id=project.id, user_id=project.user_id, lane="frontend",
        status=DevSessionStatus.RUNNING,
    )
    db.session.add(s)
    db.session.commit()
    return s


def test_fold_asset_validation_forces_ac(app):
    from backend.services.agent.workflows.code_dev_turn_workflow import _fold_asset_validation
    from backend.services.code import dev_sprint_service as svc

    session = _mk_session(app)
    task = _asset_task(session)
    feats = svc.features_from_dev_tasks(session.id, focus_task=task)
    # Reviewer optimistically passed everything — the validator says the file is missing.
    for f in feats:
        f["passes"] = True
    folded = _fold_asset_validation(
        feats, task, {"ok": False, "reason": "必需资源缺失:src/assets/hero.png", "outputs": []},
    )
    assert all(not f["passes"] for f in folded if f["id"].startswith("ASSET.FR1.1"))
    # And the deterministic pass once the files are verified on disk.
    folded = _fold_asset_validation(
        feats, task,
        {"ok": True, "reason": "", "outputs": [{"path": "src/assets/hero.png", "exists": True}]},
    )
    assert all(f["passes"] for f in folded if f["id"].startswith("ASSET.FR1.1"))


def test_asset_outcome_retry_then_blocked(app):
    """Missing outputs burn the retry budget (pending -> blocked), mirroring the
    doc's asset state-machine mapping."""
    from backend.services.agent.workflows.code_dev_turn_workflow import _fold_asset_validation
    from backend.services.code import dev_sprint_service as svc

    session = _mk_session(app)
    task = _asset_task(session)
    task.max_retries = 1
    db.session.commit()
    feats = svc.features_from_dev_tasks(session.id, focus_task=task)
    folded = _fold_asset_validation(feats, task, {"ok": False, "reason": "缺失", "outputs": []})
    outcome = svc.apply_verify_outcome(task, "run-1", folded, False)
    assert outcome["status"] == DevTaskStatus.PENDING  # retryable
    db.session.expire_all()
    task = db.session.get(CodeDevTask, task.id)
    assert task.effective_retry_count == 1
    # Second failed attempt exhausts the budget.
    svc.mark_queued(task.id)
    svc.mark_in_progress(task.id, "run-2")
    svc.mark_verifying(task.id)
    outcome = svc.apply_verify_outcome(task, "run-2", folded, False)
    assert outcome["status"] == DevTaskStatus.BLOCKED


def test_asset_blocking_maps_to_blocked_without_retry(app):
    """An env-dead validation blocks straight from VERIFYING (the workflow path),
    without touching retry_count."""
    from backend.services.code import dev_sprint_service as svc

    session = _mk_session(app)
    task = _asset_task(session)
    assert svc.mark_blocked(task.id, "资源生成环境不可用:未配置 OPENAI_API_KEY")
    db.session.expire_all()
    task = db.session.get(CodeDevTask, task.id)
    assert task.status == DevTaskStatus.BLOCKED
    assert task.effective_retry_count == 0
    assert "OPENAI_API_KEY" in task.blocked_reason


def test_image_env_flags_exclude_secret(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("OPENAI_IMAGE_MODEL", "gpt-image-2")
    monkeypatch.setenv("FE_CODEX_TIMEOUT", "600")
    flags = asset_lane.docker_env_flags()
    joined = " ".join(flags)
    assert "OPENAI_IMAGE_MODEL=gpt-image-2" in joined
    assert "FE_CODEX_TIMEOUT=600" in joined
    assert "sk-secret" not in joined  # the key is passed by NAME by the caller


def test_write_asset_context_pipes_stdin(app, monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(dev_service_mod.subprocess, "run", fake_run)
    ok = DevService().write_asset_context("pid", "风格上下文" * 10)
    assert ok
    assert b"\xe9\xa3\x8e\xe6\xa0\xbc" in captured["input"]  # UTF-8 风格
    assert "style_context.txt" in captured["cmd"][-1]


def test_subprocess_import_is_module_level():
    """The fakes above patch dev_service.subprocess.run — pin that the module
    really uses the module-level import (not a from-import copy)."""
    assert dev_service_mod.subprocess is subprocess
