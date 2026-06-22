"""
GitHub auto-sync (Code domain).

Assembles a Code session's current deliverables (docs + generated frontend source
+ optional built dist + a generated README) and pushes them to the session's
GitHub repository as one commit via the Git Data API — a full-state snapshot, so
each completed generation run becomes one commit and removed files disappear.

``autosync_after_run`` is the single entry point called by the agent runtime when
a ``code_*`` run completes. It is intentionally non-fatal: any failure is recorded
in a ``GitHubPushLog`` and surfaced as a ``GITHUB_SYNC`` event, but never raises
into the run lifecycle.
"""
import logging
import os
import re
import zipfile
from datetime import datetime

from backend.extensions import db
from backend.models.agent import AgentArtifact, AgentEventLevel, AgentEventType
from backend.models.code import (
    CodeProject,
    GitHubPushLog,
    GitHubPushStatus,
    GitHubRepoLink,
)
from backend.services.agent.files import upload_root
from backend.services.code.github import app_auth
from backend.services.code.github.client import GitHubClient, GitHubError

logger = logging.getLogger(__name__)

# Skip pathologically large individual files (keeps a commit sane).
_MAX_FILE_BYTES = 4 * 1024 * 1024


# --- naming helpers ----------------------------------------------------------
def _slug(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", (text or "").strip().lower()).strip("-")
    return slug[:80] or "project"


def repo_name_for(project: CodeProject) -> str:
    prefix = os.getenv("GITHUB_REPO_PREFIX", "")
    return f"{prefix}{_slug(project.title)}-{project.id[:8]}"


# --- file collection ---------------------------------------------------------
def _latest_artifact(project_id: str, domain_ref_type: str):
    return (
        AgentArtifact.query.filter_by(
            domain_ref_type=domain_ref_type, domain_ref_id=project_id
        )
        .order_by(AgentArtifact.created_at.desc())
        .first()
    )


def _readme(project: CodeProject, has_frontend: bool) -> str:
    lines = [
        f"# {project.title}",
        "",
        "> 本仓库由 AI Creative Studio 按会话自动生成与同步。每次生成阶段对应一次提交。",
        "",
        "## 需求",
        "",
        (project.requirement_input or "").strip() or "_(未提供)_",
        "",
    ]
    if has_frontend:
        lines += [
            "## 技术栈",
            "",
            "React + Vite + TypeScript 多文件前端工程。",
            "",
            "## 本地运行",
            "",
            "```bash",
            "npm install",
            "npm run dev",
            "```",
            "",
        ]
    lines += [
        "## 目录",
        "",
        "- `docs/` — 需求 / 开发流程 / 风格 / 拆分文档",
        "- 仓库根 — 生成的前端工程源码" if has_frontend else "- (前端工程源码将在前端生成完成后同步)",
    ]
    if _push_dist_enabled():
        lines.append("- `dist/` — 构建产物")
    return "\n".join(lines) + "\n"


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


def _collect_frontend_source(project_id: str) -> dict[str, bytes]:
    """Unzip the latest generated multi-file project into a path -> bytes map."""
    artifact = _latest_artifact(project_id, "code_frontend_project_zip")
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
                if info.file_size > _MAX_FILE_BYTES:
                    logger.warning("skip oversized source file %s", info.filename)
                    continue
                files[info.filename] = archive.read(info)
    except (zipfile.BadZipFile, OSError) as exc:
        logger.warning("Failed to read frontend source zip for %s: %s", project_id, exc)
        return {}
    return files


def _push_dist_enabled() -> bool:
    return os.getenv("GITHUB_PUSH_DIST", "true").lower() in ("1", "true", "yes")


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
        files[f"dist/{rel}"] = path.read_bytes()
    return files


def collect_project_files(project: CodeProject) -> dict[str, bytes]:
    """Assemble the full file set to sync: source (root) + docs/ + dist/ + README."""
    files: dict[str, bytes] = {}
    source = _collect_frontend_source(project.id)
    files.update(source)
    files.update(_collect_docs(project))
    files.update(_collect_dist(project.id))
    # Keep the project's own README if it ships one; otherwise generate one.
    if "README.md" not in files:
        files["README.md"] = _readme(project, has_frontend=bool(source)).encode("utf-8")
    return files


# --- git push ----------------------------------------------------------------
def push_snapshot(
    client: GitHubClient, link: GitHubRepoLink, files: dict[str, bytes], message: str
) -> str:
    """Commit ``files`` as a full-state snapshot on ``link.default_branch``.

    Returns the new commit sha. The tree is created WITHOUT a base_tree so the
    commit reflects exactly the given file set (deletions included).
    """
    owner, repo, branch = link.repo_owner, link.repo_name, link.default_branch
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


# --- entry point -------------------------------------------------------------
def autosync_after_run(recorder, run) -> None:
    """Sync a completed code run's session to GitHub. Never raises."""
    if not app_auth.is_configured():
        return
    project = db.session.get(CodeProject, run.resource_id)
    if not project:
        return
    files = collect_project_files(project)
    if not files:
        return

    log = GitHubPushLog(
        project_id=project.id,
        run_id=run.id,
        status=GitHubPushStatus.PENDING,
        message=f"auto-sync after {run.workflow}",
    )
    db.session.add(log)
    db.session.commit()

    recorder.emit(
        AgentEventType.GITHUB_SYNC,
        message="正在推送到 GitHub…",
        payload={"status": "pending", "files": len(files)},
    )

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
            client,
            link,
            files,
            message=f"chore: sync {run.workflow} (session {project.id[:8]}, run {run.id[:8]})",
        )

        now = datetime.utcnow()
        link.last_commit_sha = commit_sha
        link.last_pushed_at = now
        link.last_status = GitHubPushStatus.SUCCESS
        log.status = GitHubPushStatus.SUCCESS
        log.commit_sha = commit_sha
        log.files_count = len(files)
        log.finished_at = now
        db.session.commit()

        recorder.emit(
            AgentEventType.GITHUB_SYNC,
            message=f"已推送到 GitHub:{link.repo_owner}/{link.repo_name}",
            payload={
                "status": "success",
                "repo_url": link.html_url,
                "full_name": f"{link.repo_owner}/{link.repo_name}",
                "branch": link.default_branch,
                "commit_sha": commit_sha,
                "files": len(files),
            },
        )
    except Exception as exc:  # noqa: BLE001 - non-fatal: record + surface, never crash the run
        logger.error("GitHub autosync failed for run %s: %s", run.id, exc, exc_info=True)
        db.session.rollback()
        try:
            log = db.session.get(GitHubPushLog, log.id)
            if log:
                log.status = GitHubPushStatus.FAILED
                log.error_message = str(exc)
                log.finished_at = datetime.utcnow()
            link = GitHubRepoLink.query.filter_by(project_id=project.id).first()
            if link:
                link.last_status = GitHubPushStatus.FAILED
            db.session.commit()
        except Exception:  # noqa: BLE001
            logger.error("Failed to record GitHub push failure for run %s", run.id, exc_info=True)
        message = getattr(exc, "message", str(exc))
        try:
            recorder.emit(
                AgentEventType.GITHUB_SYNC,
                level=AgentEventLevel.ERROR,
                message=f"推送到 GitHub 失败:{message}",
                payload={"status": "failed", "error": message},
            )
        except Exception:  # noqa: BLE001
            logger.error("Failed to emit GitHub failure event for run %s", run.id, exc_info=True)
