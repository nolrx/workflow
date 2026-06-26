"""
GitHub auto-sync (Code domain).

Assembles a Code session's current full-stack deliverables (frontend + backend +
middleware source, the shared API contract, docs + a generated README) and pushes
them to the session's GitHub repository as one commit via the Git Data API — a
full-state snapshot, so each completed run becomes one commit and removed files
disappear.

Layout in the repo (a per-session monorepo):

    frontend/   — generated React + Vite + TS project source
    backend/    — generated polyglot backend (deploy-validated / repaired wins)
    db/         — middleware init.sql / seed.sql
    contract/   — the shared OpenAPI contract + middleware manifest
    docs/       — requirements / flow / style / split docs
    README.md, .gitignore

``autosync_after_run`` is the entry point called by the agent runtime when a
``code_*`` run completes; ``sync_project`` is the same logic exposed for a manual
re-push. Both are intentionally non-fatal: any failure is recorded in a
``GitHubPushLog`` and surfaced as a ``GITHUB_SYNC`` event, but never raises into
the run lifecycle.

Branch model: the platform only ever OWNS / overwrites ``default_branch`` (main)
with the full-state snapshot. After a successful deploy sync it forks a
``dev`` branch (``GITHUB_DEV_BRANCH``) from the validated commit for the user's
secondary development — and never touches it again, so manual edits there are
never clobbered by a later snapshot.
"""
import json
import logging
import os
import re
import zipfile
from datetime import datetime

from backend.extensions import db
from backend.models.agent import AgentArtifact, AgentEventLevel, AgentEventType
from backend.models.code import (
    CodeDeployment,
    CodeProject,
    CodeProjectLedger,
    GitHubPushLog,
    GitHubPushStatus,
    GitHubRepoLink,
)
from backend.services.agent.files import upload_root
from backend.services.code.github import app_auth
from backend.services.code.github.client import GitHubClient, GitHubError

logger = logging.getLogger(__name__)

# Skip pathologically large individual files (keeps a commit sane). Skipped files
# are reported (event payload + push log) rather than silently dropped.
_MAX_FILE_BYTES = 4 * 1024 * 1024

# The full-stack deploy run — its completion is the "validated" milestone that
# promotes the repaired backend source and forks the secondary-dev branch.
_DEPLOY_WORKFLOW = "code_fullstack_deploy"


# --- naming helpers ----------------------------------------------------------
def _slug(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", (text or "").strip().lower()).strip("-")
    return slug[:80] or "project"


def repo_name_for(project: CodeProject) -> str:
    prefix = os.getenv("GITHUB_REPO_PREFIX", "")
    return f"{prefix}{_slug(project.title)}-{project.id[:8]}"


def dev_branch_name() -> str:
    """Branch the platform forks for the user's secondary development."""
    return (os.getenv("GITHUB_DEV_BRANCH", "dev") or "dev").strip() or "dev"


def _is_deploy(workflow: str) -> bool:
    return workflow == _DEPLOY_WORKFLOW


# --- file collection ---------------------------------------------------------
def _latest_artifact(project_id: str, domain_ref_type: str):
    return (
        AgentArtifact.query.filter_by(
            domain_ref_type=domain_ref_type, domain_ref_id=project_id
        )
        .order_by(AgentArtifact.created_at.desc())
        .first()
    )


def _readme(project: CodeProject, *, has_frontend: bool, has_backend: bool, has_middleware: bool) -> str:
    lines = [
        f"# {project.title}",
        "",
        "> 本仓库由 Worksflow 按会话自动生成与同步。每次生成/部署阶段对应一次提交。",
        "",
        "## 需求",
        "",
        (project.requirement_input or "").strip() or "_(未提供)_",
        "",
        "## 目录结构",
        "",
    ]
    if has_frontend:
        lines.append("- `frontend/` — 生成的 React + Vite + TypeScript 前端工程")
    if has_backend:
        lines.append("- `backend/` — 生成的后端工程（含 Dockerfile，部署验证/自愈后的版本）")
    if has_middleware:
        lines.append("- `db/` — 中间件初始化 SQL（`init.sql` / `seed.sql`）")
    lines += [
        "- `contract/` — 前后端共享的 OpenAPI 契约与中间件清单",
        "- `docs/` — 需求 / 开发流程 / 风格 / 拆分文档",
        "",
    ]
    if has_frontend:
        lines += [
            "## 前端本地运行",
            "",
            "```bash",
            "cd frontend && npm install && npm run dev",
            "```",
            "",
        ]
    if has_backend:
        lines += [
            "## 后端本地构建",
            "",
            "```bash",
            "cd backend && docker build -t app . && docker run --rm -p 8080:8080 app",
            "```",
            "",
        ]
    lines += [
        "## 二次开发",
        "",
        f"平台仅自动覆盖 `{project_default_branch_hint()}` 主分支（每次生成/部署的全量快照）。"
        f"二次开发请基于 `{dev_branch_name()}` 分支进行 —— 平台不会触碰该分支，你的改动不会被下次同步覆盖。",
        "",
    ]
    return "\n".join(lines) + "\n"


def project_default_branch_hint() -> str:
    """Best-effort default-branch label for docs (real value lives on the link)."""
    return "main"


def _collect_docs(project: CodeProject) -> dict[str, bytes]:
    files: dict[str, bytes] = {}

    def add(path: str, text):
        if text and str(text).strip():
            files[path] = str(text).encode("utf-8")

    add("docs/requirements.md", project.requirements_doc)
    add("docs/development-flow.md", project.development_flow)
    add("docs/style.md", project.style_prompt)
    add("docs/ui-baseline.md", project.ui_baseline_prompt)
    for document in project.documents.all():
        name = f"docs/{document.order_index:02d}-{_slug(document.title)}.md"
        add(name, document.content)
    return files


def _unzip_artifact_files(artifact, *, prefix: str, skipped: list | None) -> dict[str, bytes]:
    """Unzip an on-disk zip artifact into ``{prefix+path: bytes}``.

    Oversized files are reported into ``skipped`` (not silently dropped) so the
    caller can surface them. Binary-safe (images / wasm / sourcemaps survive)."""
    if not artifact or not artifact.storage_path:
        return {}
    zip_path = upload_root() / artifact.storage_path
    if not zip_path.exists():
        return {}
    files: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(zip_path, "r") as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                rel = f"{prefix}{info.filename}"
                if info.file_size > _MAX_FILE_BYTES:
                    if skipped is not None:
                        skipped.append({"path": rel, "size": info.file_size})
                    logger.warning("skip oversized source file %s (%d bytes)", rel, info.file_size)
                    continue
                files[rel] = archive.read(info)
    except (zipfile.BadZipFile, OSError) as exc:
        logger.warning("Failed to read zip artifact %s: %s", getattr(artifact, "id", "?"), exc)
        return {}
    return files


def _collect_frontend_source(project_id: str, skipped: list | None = None) -> dict[str, bytes]:
    """Unzip the latest generated multi-file frontend project under ``frontend/``."""
    artifact = _latest_artifact(project_id, "code_frontend_project_zip")
    return _unzip_artifact_files(artifact, prefix="frontend/", skipped=skipped)


def _collect_backend_source(project_id: str, skipped: list | None = None) -> dict[str, bytes]:
    """Unzip the latest backend project under ``backend/``.

    Prefer the deploy-validated/repaired source (``code_backend_project_repaired_zip``,
    promoted by the deploy run, byte-for-byte what the live image runs) so git == what
    is actually running; fall back to the generation-time source."""
    artifact = _latest_artifact(project_id, "code_backend_project_repaired_zip") or _latest_artifact(
        project_id, "code_backend_project_zip"
    )
    return _unzip_artifact_files(artifact, prefix="backend/", skipped=skipped)


def _collect_middleware(project_id: str) -> dict[str, bytes]:
    """Collect middleware DDL/seed under ``db/`` (pure text, tiny)."""
    art = _latest_artifact(project_id, "code_middleware_meta")
    if art:
        meta = art.get_content_json() or {}
        out: dict[str, bytes] = {}
        init_sql = (meta.get("init_sql") or "").strip()
        seed_sql = (meta.get("seed_sql") or "").strip()
        if init_sql:
            out["db/init.sql"] = init_sql.encode("utf-8")
        if seed_sql:
            out["db/seed.sql"] = seed_sql.encode("utf-8")
        if out:
            return out
    # Fall back to the standalone combined init.sql text artifact.
    sql_art = _latest_artifact(project_id, "code_middleware_sql")
    if sql_art and sql_art.content_text and sql_art.content_text.strip():
        return {"db/init.sql": sql_art.content_text.encode("utf-8")}
    return {}


def _collect_contract(project_id: str) -> dict[str, bytes]:
    """Collect the shared OpenAPI contract + middleware manifest under ``contract/``."""
    ledger = CodeProjectLedger.query.filter_by(project_id=project_id).first()
    if not ledger:
        return {}
    out: dict[str, bytes] = {}
    contract = ledger.get_api_contract()
    if contract:
        out["contract/openapi.json"] = json.dumps(
            contract, ensure_ascii=False, indent=2
        ).encode("utf-8")
    manifest = ledger.get_middleware_manifest()
    if manifest:
        out["contract/middleware.json"] = json.dumps(
            manifest, ensure_ascii=False, indent=2
        ).encode("utf-8")
    return out


def _push_dist_enabled() -> bool:
    # Default OFF: dist is a build product, bloats the repo, and git should hold source.
    return os.getenv("GITHUB_PUSH_DIST", "false").lower() in ("1", "true", "yes")


def _collect_dist(project_id: str) -> dict[str, bytes]:
    """Read the built dist for the project from its build run's on-disk site dir."""
    if not _push_dist_enabled():
        return {}
    artifact = _latest_artifact(project_id, "code_frontend_project_zip")
    if not artifact:
        return {}
    site_dir = upload_root() / "agent_runs" / artifact.run_id / "site"
    if not site_dir.is_dir():
        return {}
    files: dict[str, bytes] = {}
    for path in site_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.stat().st_size > _MAX_FILE_BYTES:
            continue
        rel = path.relative_to(site_dir).as_posix()
        files[f"frontend/dist/{rel}"] = path.read_bytes()
    return files


def _gitignore() -> bytes:
    return (
        "# Auto-generated by Worksflow.\n"
        "node_modules/\n"
        "dist/\n"
        "build/\n"
        ".venv/\n"
        "venv/\n"
        "__pycache__/\n"
        "*.pyc\n"
        "target/\n"
        "vendor/\n"
        "*.log\n"
        ".env\n"
        ".env.*\n"
        ".DS_Store\n"
    ).encode("utf-8")


def collect_project_files(project: CodeProject, skipped: list | None = None) -> dict[str, bytes]:
    """Assemble the full file set to sync: frontend/ + backend/ + db/ + contract/ + docs/.

    Idempotent full-state snapshot of the project's CURRENT deliverables — each
    component is included if its artifact exists, always preferring the validated
    version. ``skipped`` (if given) accumulates oversized files that were dropped."""
    files: dict[str, bytes] = {}
    frontend = _collect_frontend_source(project.id, skipped)
    backend = _collect_backend_source(project.id, skipped)
    middleware = _collect_middleware(project.id)
    files.update(frontend)
    files.update(backend)
    files.update(middleware)
    files.update(_collect_contract(project.id))
    files.update(_collect_docs(project))
    files.update(_collect_dist(project.id))
    if ".gitignore" not in files:
        files[".gitignore"] = _gitignore()
    files["README.md"] = _readme(
        project,
        has_frontend=bool(frontend),
        has_backend=bool(backend),
        has_middleware=bool(middleware),
    ).encode("utf-8")
    return files


# --- git push ----------------------------------------------------------------
def push_snapshot(
    client: GitHubClient,
    link: GitHubRepoLink,
    files: dict[str, bytes],
    message: str,
    branch: str | None = None,
) -> str:
    """Commit ``files`` as a full-state snapshot on ``branch`` (default: the link's
    default branch).

    Returns the new commit sha. The tree is created WITHOUT a base_tree so the
    commit reflects exactly the given file set (deletions included).
    """
    owner, repo = link.repo_owner, link.repo_name
    branch = branch or link.default_branch
    ref = f"heads/{branch}"
    existing = client.get_ref(owner, repo, ref)
    parent_sha = existing["object"]["sha"] if existing else None

    tree = []
    for path, content in sorted(files.items()):
        blob_sha = client.create_blob(owner, repo, content)
        tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob_sha})

    tree_obj = client.create_tree(owner, repo, tree)
    commit = client.create_commit(
        owner, repo, message, tree_obj["sha"], parents=[parent_sha] if parent_sha else []
    )
    commit_sha = commit["sha"]
    if existing:
        client.update_ref(owner, repo, ref, commit_sha, force=False)
    else:
        client.create_ref(owner, repo, f"refs/heads/{branch}", commit_sha)
    return commit_sha


def _ensure_dev_branch(client: GitHubClient, link: GitHubRepoLink, base_sha: str) -> str | None:
    """Fork the secondary-dev branch from ``base_sha`` if it does not exist yet.

    Idempotent and STRICTLY non-destructive: if the branch already exists we never
    touch it, so the user's secondary development is never clobbered by a later
    platform snapshot."""
    branch = dev_branch_name()
    if branch == link.default_branch:
        return None
    try:
        if client.get_ref(link.repo_owner, link.repo_name, f"heads/{branch}"):
            return branch  # already exists — leave human work untouched
        client.create_ref(link.repo_owner, link.repo_name, f"refs/heads/{branch}", base_sha)
        return branch
    except GitHubError as exc:
        logger.warning("ensure dev branch failed for %s/%s: %s", link.repo_owner, link.repo_name, exc)
        return None


def _commit_message(project: CodeProject, workflow: str, run_id: str | None) -> str:
    sid = project.id[:8]
    rid = (run_id or "manual")[:8]
    if _is_deploy(workflow):
        dep = CodeDeployment.query.filter_by(project_id=project.id).first()
        tag = (dep.image_tag if dep else None) or "?"
        return f"deploy: 已验证全栈源码 (image {tag}, session {sid}, run {rid})"
    return f"chore: sync {workflow} (session {sid}, run {rid})"


def _find_or_create_link(
    project: CodeProject, installation_id: str, owner: str, client: GitHubClient
) -> GitHubRepoLink:
    link = GitHubRepoLink.query.filter_by(project_id=project.id).first()
    if link:
        return link

    name = repo_name_for(project)
    visibility = os.getenv("GITHUB_REPO_VISIBILITY", "private")
    repo = client.get_repo(owner, name)
    if not repo:
        repo = client.create_org_repo(
            owner,
            name,
            private=visibility != "public",
            description=(project.title or "")[:300],
        )
    link = GitHubRepoLink(
        project_id=project.id,
        user_id=project.user_id,
        team_id=project.team_id,
        installation_id=installation_id,
        repo_owner=owner,
        repo_name=repo.get("name", name),
        repo_id=str(repo.get("id")) if repo.get("id") is not None else None,
        default_branch=repo.get("default_branch") or "main",
        html_url=repo.get("html_url"),
        visibility=visibility,
    )
    db.session.add(link)
    db.session.commit()
    return link


# --- sync core ---------------------------------------------------------------
def _perform_sync(project: CodeProject, *, run_id: str | None, workflow: str, recorder=None) -> dict:
    """Push the project's current deliverables to its repo as one snapshot.

    Non-fatal: records a ``GitHubPushLog`` and (when a recorder is given) emits
    ``GITHUB_SYNC`` events on every path; never raises. Returns a result dict for
    the manual-sync endpoint."""
    skipped: list = []
    files = collect_project_files(project, skipped=skipped)
    if not files:
        return {"status": "skipped", "reason": "no_files"}

    log = GitHubPushLog(
        project_id=project.id,
        run_id=run_id,
        status=GitHubPushStatus.PENDING,
        message=f"sync after {workflow}",
    )
    db.session.add(log)
    db.session.commit()

    def _emit(level=None, **kwargs):
        if recorder is None:
            return
        try:
            if level is None:
                recorder.emit(AgentEventType.GITHUB_SYNC, **kwargs)
            else:
                recorder.emit(AgentEventType.GITHUB_SYNC, level=level, **kwargs)
        except Exception:  # noqa: BLE001
            logger.error("Failed to emit GITHUB_SYNC event for run %s", run_id, exc_info=True)

    _emit(message="正在推送到 GitHub…", payload={"status": "pending", "files": len(files)})

    try:
        installation_id, account = app_auth.resolve_installation()
        owner = app_auth.repo_owner(account)
        if not owner:
            raise GitHubError(500, "GITHUB_NOT_CONFIGURED", "无法确定目标 GitHub 组织/账户")
        token = app_auth.get_installation_token(installation_id)
        client = GitHubClient(token, app_auth.api_base())

        link = _find_or_create_link(project, installation_id, owner, client)
        log.repo_link_id = link.id
        log.branch = link.default_branch
        commit_sha = push_snapshot(
            client, link, files, _commit_message(project, workflow, run_id)
        )

        # The deploy run is the "validated" milestone: fork the secondary-dev
        # branch from this commit (once), never overwrite it afterwards.
        dev_branch = _ensure_dev_branch(client, link, commit_sha) if _is_deploy(workflow) else None

        now = datetime.utcnow()
        link.last_commit_sha = commit_sha
        link.last_pushed_at = now
        link.last_status = GitHubPushStatus.SUCCESS
        log.status = GitHubPushStatus.SUCCESS
        log.commit_sha = commit_sha
        log.files_count = len(files)
        log.finished_at = now
        db.session.commit()

        payload = {
            "status": "success",
            "repo_url": link.html_url,
            "full_name": f"{link.repo_owner}/{link.repo_name}",
            "branch": link.default_branch,
            "commit_sha": commit_sha,
            "files": len(files),
        }
        if dev_branch:
            payload["dev_branch"] = dev_branch
        if skipped:
            payload["skipped"] = skipped

        msg = f"已推送到 GitHub:{link.repo_owner}/{link.repo_name}"
        if dev_branch:
            msg += f"，二次开发分支 {dev_branch} 已就绪"
        if skipped:
            msg += f"(跳过 {len(skipped)} 个超大文件)"
        _emit(message=msg, payload=payload)
        return payload
    except Exception as exc:  # noqa: BLE001 - non-fatal: record + surface, never crash
        logger.error("GitHub sync failed for project %s: %s", project.id, exc, exc_info=True)
        db.session.rollback()
        message = getattr(exc, "message", str(exc))
        try:
            log = db.session.get(GitHubPushLog, log.id)
            if log:
                log.status = GitHubPushStatus.FAILED
                log.error_message = message
                log.finished_at = datetime.utcnow()
            link = GitHubRepoLink.query.filter_by(project_id=project.id).first()
            if link:
                link.last_status = GitHubPushStatus.FAILED
            db.session.commit()
        except Exception:  # noqa: BLE001
            logger.error("Failed to record GitHub push failure for project %s", project.id, exc_info=True)
        _emit(level=AgentEventLevel.ERROR, message=f"推送到 GitHub 失败:{message}",
              payload={"status": "failed", "error": message})
        return {"status": "failed", "error": message}


# --- entry points ------------------------------------------------------------
def autosync_after_run(recorder, run) -> None:
    """Sync a completed code run's session to GitHub. Never raises."""
    if not app_auth.is_configured():
        return
    project = db.session.get(CodeProject, run.resource_id)
    if not project:
        return
    try:
        _perform_sync(project, run_id=run.id, workflow=run.workflow, recorder=recorder)
    except Exception:  # noqa: BLE001 - belt-and-suspenders; _perform_sync already guards
        logger.error("autosync_after_run unexpected failure for run %s", run.id, exc_info=True)


def sync_project(project: CodeProject) -> dict:
    """Manually (re)push a session's current deliverables. Non-fatal; returns a
    result dict. Used by the manual-sync endpoint as a self-service retry."""
    if not app_auth.is_configured():
        return {"status": "unconfigured"}
    return _perform_sync(project, run_id=None, workflow="manual", recorder=None)
