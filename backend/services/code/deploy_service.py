"""
Atomic full-stack deploy service.

Brings a generated app up behind a reverse proxy in dependency order, with
rollback on any failure (so a half-deployed app never lingers):

    provision middleware namespace  (CREATE DATABASE app_<pid> + redis prefix)
      → apply generated init.sql      (best-effort fallback for non-self-migrating backends)
      → docker build the generated backend image (its OWN Dockerfile)
      → docker run a long-lived container on the shared app network
      → health-check  GET http://app-<pid>:<port>/health
      → register the deployment  (the /app/<pid>/api reverse proxy resolves here)

The backend container joins the same docker network as this platform's backend,
so it is reachable by name (no published host port). The generated frontend is
served unchanged at /preview/<pid>/; the preview route injects a runtime API base
(``window.__API_BASE__ = "/app/<pid>/api"``) so the static build talks to THIS
backend without a rebuild. Comments in English (Code/core convention).
"""
import logging
import os
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from backend.extensions import db
from backend.models.agent import AgentArtifact, AgentRun, AgentRunStatus
from backend.models.code import CodeProject
from backend.models.code.fullstack import CodeDeployment, DeploymentStatus
from backend.services.agent.files import artifact_abs_path
from backend.services.code import middleware_service
from backend.services.code.backend_project_service import get_backend_project_service

logger = logging.getLogger(__name__)

DOCKER_BIN = os.getenv("DOCKER_BIN", "docker")
APP_NETWORK = os.getenv("APP_NETWORK", "ai-creative-studio-net")
BACKEND_PORT = int(os.getenv("APP_BACKEND_PORT", "8080"))
BUILD_TIMEOUT = int(os.getenv("APP_BUILD_TIMEOUT", "900"))
HEALTH_TIMEOUT = int(os.getenv("APP_HEALTH_TIMEOUT", "90"))
HEALTH_PATH = os.getenv("APP_HEALTH_PATH", "/health")
CONTAINER_MEM = os.getenv("APP_CONTAINER_MEM", "512m")
# Smoke stage: after /health passes, hit a few idempotent no-param GET endpoints
# sampled from the shared contract to confirm the routes actually resolve (not a
# 5xx / crash). PER-ENDPOINT timeout (not a total window split N ways) so a cold
# first hit isn't starved; a connection timeout is recorded but does NOT roll back
# a /health-green deploy — only a definitive 5xx does.
SMOKE_TIMEOUT = int(os.getenv("APP_SMOKE_TIMEOUT", "15"))  # per-endpoint, seconds
SMOKE_MAX_ENDPOINTS = int(os.getenv("APP_SMOKE_MAX_ENDPOINTS", "3"))

_BACKEND_WORKFLOW = "code_backend_project_generation"
_MIDDLEWARE_WORKFLOW = "code_middleware_provisioning"
_FRONTEND_WORKFLOW = "code_frontend_project_generation"
_BUILT = (AgentRunStatus.COMPLETED, AgentRunStatus.PARTIAL)


def _container_name(project_id: str) -> str:
    return f"app-{middleware_service._sanitized_db_name(project_id)[4:]}"[:60] or f"app-{project_id[:12]}"


def _image_tag(project_id: str) -> str:
    return f"app-{middleware_service._sanitized_db_name(project_id)[4:]}:latest"


# --- docker helpers ----------------------------------------------------------
def _docker(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [DOCKER_BIN, *args], capture_output=True, text=True, timeout=timeout
    )


def docker_available() -> bool:
    try:
        return _docker(["version", "--format", "{{.Server.Version}}"], 15).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _ensure_network() -> None:
    """Create the shared app network if it doesn't exist (idempotent)."""
    try:
        check = _docker(["network", "inspect", APP_NETWORK], 15)
        if check.returncode != 0:
            _docker(["network", "create", APP_NETWORK], 30)
    except Exception:  # noqa: BLE001
        logger.warning("could not ensure app network %s", APP_NETWORK)


def _remove_container(name: str) -> None:
    try:
        _docker(["rm", "-f", name], 30)
    except Exception:  # noqa: BLE001
        pass


# --- artifact loading --------------------------------------------------------
def _latest_run(project_id: str, user_id: str, workflow: str) -> Optional[AgentRun]:
    return (
        AgentRun.query.filter_by(resource_id=project_id, user_id=user_id, workflow=workflow)
        .filter(AgentRun.status.in_(_BUILT))
        .order_by(AgentRun.created_at.desc())
        .first()
    )


def _load_backend_source(run: AgentRun) -> dict:
    """Extract the backend source ({rel: bytes}) from the run's published zip."""
    zip_art = (
        AgentArtifact.query.filter_by(run_id=run.id, domain_ref_type="code_backend_project_zip")
        .order_by(AgentArtifact.created_at.desc())
        .first()
    )
    if not zip_art or not zip_art.storage_path:
        return {}
    abs_path = artifact_abs_path(zip_art.storage_path)
    if not os.path.exists(abs_path):
        return {}
    files: dict[str, bytes] = {}
    with zipfile.ZipFile(abs_path, "r") as archive:
        for name in archive.namelist():
            if name.endswith("/"):
                continue
            files[name] = archive.read(name)
    return files


def _load_init_sql(run: Optional[AgentRun]) -> str:
    if not run:
        return ""
    meta = (
        AgentArtifact.query.filter_by(run_id=run.id, domain_ref_type="code_middleware_meta")
        .order_by(AgentArtifact.created_at.desc())
        .first()
    )
    if not meta:
        return ""
    data = meta.get_content_json() or {}
    return (data.get("init_sql") or "") + "\n" + (data.get("seed_sql") or "")


# --- deployment registry helpers ---------------------------------------------
def get_deployment(project_id: str) -> Optional[CodeDeployment]:
    return CodeDeployment.query.filter_by(project_id=project_id).first()


def _upsert_deployment(project: CodeProject, user_id: str, team_id: Optional[str]) -> CodeDeployment:
    dep = get_deployment(project.id)
    if dep is None:
        dep = CodeDeployment(project_id=project.id, user_id=user_id, team_id=team_id)
        db.session.add(dep)
    dep.user_id = user_id
    dep.team_id = team_id
    dep.status = DeploymentStatus.PENDING
    dep.error_message = None
    db.session.commit()
    return dep


def resolve_proxy_target(project_id: str, user_id: str) -> Optional[tuple[str, int]]:
    """(container_name, port) for the /app/<pid>/api proxy, or None if not running."""
    dep = get_deployment(project_id)
    if (
        not dep
        or dep.user_id != user_id
        or dep.status != DeploymentStatus.RUNNING
        or not dep.container_name
    ):
        return None
    return dep.container_name, dep.internal_port or BACKEND_PORT


# --- the atomic deploy -------------------------------------------------------
def deploy(
    project: CodeProject,
    user_id: str,
    team_id: Optional[str] = None,
    on_phase: Optional[Callable[[str, str, dict], None]] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> dict:
    """Atomically deploy the generated full-stack app. Returns a result dict.

    ``on_phase(phase, message, payload)`` narrates progress to the deploy run's
    timeline. Rolls back (stop container, drop database) on any failure.
    """
    def phase(name: str, message: str, payload: Optional[dict] = None) -> None:
        if on_phase:
            try:
                on_phase(name, message, payload or {})
            except Exception:  # noqa: BLE001
                logger.exception("deploy on_phase raised")

    def cancelled() -> bool:
        return bool(is_cancelled and is_cancelled())

    if not docker_available():
        return {"success": False, "error": "docker 不可用,无法部署生成的后端", "status": DeploymentStatus.FAILED}

    backend_run = _latest_run(project.id, user_id, _BACKEND_WORKFLOW)
    if not backend_run:
        return {"success": False, "error": "尚无已完成的后端工程,无法部署", "status": DeploymentStatus.FAILED}
    frontend_run = _latest_run(project.id, user_id, _FRONTEND_WORKFLOW)
    middleware_run = _latest_run(project.id, user_id, _MIDDLEWARE_WORKFLOW)

    source = _load_backend_source(backend_run)
    if not source:
        return {"success": False, "error": "后端源码产物缺失,无法部署", "status": DeploymentStatus.FAILED}

    dep = _upsert_deployment(project, user_id, team_id)
    dep.backend_run_id = backend_run.id
    dep.frontend_run_id = frontend_run.id if frontend_run else None
    dep.middleware_run_id = middleware_run.id if middleware_run else None
    container = _container_name(project.id)
    image_tag = _image_tag(project.id)
    dep.container_name = container
    dep.image_tag = image_tag
    dep.api_base_path = f"/app/{project.id}/api"
    db.session.commit()

    rollback: list[Callable[[], None]] = []
    workdir: Optional[Path] = None
    try:
        _ensure_network()

        # --- 1. Provision middleware namespace -------------------------------
        if cancelled():
            return _abort(dep, rollback, "已取消")
        dep.status = DeploymentStatus.PROVISIONING
        db.session.commit()
        phase("provision", "创建项目专属中间件命名空间(数据库 / 缓存前缀)")
        prov = middleware_service.provision_namespace(project.id)
        if not prov.applicable:
            return _fail(dep, rollback, f"中间件命名空间创建失败:{prov.error}", narrate=phase)
        if prov.db_name:
            rollback.append(lambda: middleware_service.teardown_namespace(prov.db_name))
        dep.db_name = prov.db_name
        dep.redis_prefix = prov.redis_prefix
        db.session.commit()
        phase("provision", f"中间件就绪:{prov.engine_kind}" + (f" / {prov.db_name}" if prov.db_name else ""),
              {"middleware": prov.to_dict()})

        # --- 2. Migrate the data layer (distinct, observable CI stage) --------
        # The generated backend self-migrates on boot where possible; this applies
        # the generated init.sql as a fallback for non-self-migrating backends.
        # Best-effort (never sinks a deploy on its own — per-statement errors are
        # tolerated; the backend may also create tables on boot).
        if cancelled():
            return _abort(dep, rollback, "已取消")
        phase("migrate", "执行数据层迁移(优先后端自迁移;应用生成的 init.sql 兜底)")
        init_sql = _load_init_sql(middleware_run)
        init_applied = bool(
            init_sql.strip()
            and prov.database_url
            and not prov.database_url.startswith("sqlite")
        )
        if init_applied:
            ok, log = middleware_service.apply_init_sql(prov.database_url, init_sql)
            phase("migrate", f"数据层就绪:{log}", {"applied": ok})
        else:
            phase("migrate", "无 init.sql 兜底;依赖后端启动时自建表/自迁移", {"applied": True})

        # --- 3. Build (=package) the backend image (its own Dockerfile) ------
        if cancelled():
            return _abort(dep, rollback, "已取消")
        dep.status = DeploymentStatus.BUILDING
        db.session.commit()
        phase("build", "构建后端镜像(docker build 工程自带 Dockerfile)")
        workdir = Path(tempfile.mkdtemp(prefix="be-deploy-"))
        os.chmod(workdir, 0o777)
        _stage_source(workdir, source)
        if not (workdir / "Dockerfile").exists():
            return _fail(dep, rollback, "后端工程缺少 Dockerfile,无法构建", narrate=phase)

        # Build → AI self-heal → rebuild ladder: a failed `docker build` feeds its
        # log to the be-agent, which compiles/builds the staged source for real
        # (the image now carries JDK+Maven / Go / Python / Node toolchains) and
        # edits it in place until green (mirrors the frontend self-healing ladder,
        # here at deploy time). Bounded by APP_BUILD_REPAIRS (default 3) — polyglot
        # compile-error cascades typically need a few rounds, since fixing one
        # error surfaces the next. Repair is skipped when no provider is
        # configured; the original failure is then reported and rolled back.
        def _repair_log(message: str) -> None:
            phase("build", message)

        build = _docker(["build", "-t", image_tag, str(workdir)], BUILD_TIMEOUT)
        max_repairs = int(os.getenv("APP_BUILD_REPAIRS", "3"))
        attempt = 0
        while build.returncode != 0 and attempt < max_repairs:
            if cancelled():
                return _abort(dep, rollback, "已取消")
            attempt += 1
            log_tail = (build.stderr or build.stdout or "")[-7000:]
            phase("build", f"镜像构建失败,启动 AI 定向修复(第 {attempt}/{max_repairs} 轮)", {"attempt": attempt})
            rep = get_backend_project_service().repair_build(
                workdir=str(workdir), build_log=log_tail,
                on_log=_repair_log, is_cancelled=cancelled,
            )
            if not rep.get("ran"):
                phase("build", "AI 修复不可用(未配置密钥),跳过")
                break
            build = _docker(["build", "-t", image_tag, str(workdir)], BUILD_TIMEOUT)
            if build.returncode == 0:
                phase("build", f"AI 修复后构建成功(第 {attempt} 轮)", {"repaired": True})
                dep.set_detail({**dep.get_detail(), "build_repaired_rounds": attempt})
                db.session.commit()
        if build.returncode != 0:
            tail = (build.stderr or build.stdout or "")[-2500:]
            dep.set_detail({**dep.get_detail(), "build_log": tail, "build_repair_attempts": attempt})
            db.session.commit()
            suffix = f"(含 {attempt} 轮 AI 修复)" if attempt else ""
            return _fail(dep, rollback, f"后端镜像构建失败{suffix}:\n{tail[-800:]}", narrate=phase)
        rollback.append(lambda: _docker(["image", "rm", "-f", image_tag], 60))
        phase("build", "后端镜像构建成功", {"image": image_tag})

        # --- 4. Run the long-lived container ---------------------------------
        if cancelled():
            return _abort(dep, rollback, "已取消")
        dep.status = DeploymentStatus.STARTING
        db.session.commit()
        phase("start", "启动后端容器并接入共享网络")
        _remove_container(container)  # clear any stale container from a prior deploy
        run_args = [
            "run", "-d", "--name", container,
            "--network", APP_NETWORK,
            "--restart", "unless-stopped",
            "--memory", CONTAINER_MEM,
            "-e", f"PORT={BACKEND_PORT}",
            "-e", "NODE_ENV=production",
        ]
        if prov.database_url:
            run_args += ["-e", f"DATABASE_URL={prov.database_url}"]
        if prov.redis_url:
            run_args += ["-e", f"REDIS_URL={prov.redis_url}"]
        if prov.redis_prefix:
            run_args += ["-e", f"REDIS_PREFIX={prov.redis_prefix}"]
        run_args += [image_tag]
        run = _docker(run_args, 120)
        if run.returncode != 0:
            return _fail(dep, rollback, f"后端容器启动失败:{(run.stderr or run.stdout)[-600:]}", narrate=phase)
        rollback.append(lambda: _remove_container(container))
        dep.internal_port = BACKEND_PORT
        db.session.commit()

        # --- 5. Health check -------------------------------------------------
        phase("health", f"健康检查 GET {HEALTH_PATH}(最长 {HEALTH_TIMEOUT}s)")
        healthy, detail = _wait_healthy(container, BACKEND_PORT, HEALTH_TIMEOUT, cancelled)

        # init.sql-as-fallback recovery: the migrate phase pre-applies the
        # middleware-generated init.sql, but a SELF-migrating backend builds its
        # own schema on boot (create_all / AutoMigrate / etc.). When the two
        # schemas drift (e.g. init.sql uses SERIAL ids / omits created_at while the
        # ORM expects uuid + timestamps), create_all skips the already-existing
        # tables and the backend then crashes querying columns that don't exist —
        # never binding its port. Give it a clean shot: reset the db to empty and
        # let the backend self-migrate. Only meaningful when init.sql was applied
        # (otherwise the first attempt already ran against an empty db).
        if not healthy and init_applied and not cancelled():
            phase("health",
                  f"健康检查未通过({detail});疑似预置 init.sql 与后端自迁移冲突,"
                  "重置为空库后让后端自建表重试")
            _docker(["stop", container], 60)
            ok_reset, reset_log = middleware_service.reset_namespace(prov.database_url)
            phase("health", f"已重置中间件命名空间为空库:{reset_log}", {"reset": ok_reset})
            started = _docker(["start", container], 120)
            if started.returncode == 0:
                healthy, detail = _wait_healthy(container, BACKEND_PORT, HEALTH_TIMEOUT, cancelled)
                if healthy:
                    dep.set_detail({**dep.get_detail(),
                                    "init_sql_skipped": True,
                                    "recovery": "reset-empty-db; backend self-migrated"})
                    db.session.commit()
                    phase("health", "重置空库后健康检查通过:后端自迁移建表成功(已跳过冲突的 init.sql)")
            else:
                phase("health", f"重置后容器重启失败:{(started.stderr or started.stdout)[-300:]}")

        if not healthy:
            logs = _docker(["logs", "--tail", "60", container], 30)
            dep.set_detail({**dep.get_detail(), "health": detail, "container_logs": (logs.stdout or logs.stderr or "")[-2000:]})
            db.session.commit()
            return _fail(dep, rollback, f"后端健康检查未通过:{detail}", narrate=phase)
        dep.health = "healthy"

        # --- 6. Smoke test (contract liveness) -------------------------------
        # /health proved the process is up; now hit a few idempotent no-param GET
        # endpoints from the shared contract to catch obvious route/contract drift
        # before exposing the app. Only a 5xx rolls back (a slow cold first hit /
        # connection timeout is recorded but tolerated — /health already passed).
        if cancelled():
            return _abort(dep, rollback, "已取消")
        phase("smoke", "契约冒烟:抽样调用幂等 GET 端点确认路由可达")
        smoke_ok, smoke_detail = _smoke_test(container, BACKEND_PORT, project.id, cancelled)
        dep.set_detail({**dep.get_detail(), "smoke": smoke_detail})
        db.session.commit()
        if not smoke_ok:
            logs = _docker(["logs", "--tail", "60", container], 30)
            dep.set_detail({**dep.get_detail(), "container_logs": (logs.stdout or logs.stderr or "")[-2000:]})
            db.session.commit()
            return _fail(dep, rollback, f"契约冒烟未通过(抽样端点返回 5xx):{smoke_detail}", narrate=phase)
        phase("smoke", "契约冒烟通过(抽样端点路由可达)", {"smoke": smoke_detail})

        # A cancel landing mid-smoke leaves _smoke_test returning ok=True (it just
        # breaks the loop), so re-check here before committing the deploy as RUNNING
        # — otherwise a late cancel would be swallowed and the app registered anyway.
        if cancelled():
            return _abort(dep, rollback, "已取消")

        # --- 7. Register (the proxy resolves here) ---------------------------
        dep.status = DeploymentStatus.RUNNING
        dep.deploy_run_id = None  # set by the workflow caller if applicable
        dep.deployed_at = datetime.utcnow()
        db.session.commit()
        phase("done", "部署完成:前端已可实时调用后端", {"api_base": dep.api_base_path})
        if workdir:
            shutil.rmtree(workdir, ignore_errors=True)
        return {
            "success": True,
            "status": DeploymentStatus.RUNNING,
            "container": container,
            "image": image_tag,
            "api_base": dep.api_base_path,
            "preview_url": f"/preview/{project.id}/",
            "middleware": prov.to_dict(),
        }
    except subprocess.TimeoutExpired as error:
        return _fail(dep, rollback, f"部署超时:{error}", narrate=phase)
    except Exception as error:  # noqa: BLE001
        logger.error("deploy failed for %s: %s", project.id, error, exc_info=True)
        return _fail(dep, rollback, f"部署异常:{error}", narrate=phase)
    finally:
        if workdir and workdir.exists() and dep.status != DeploymentStatus.RUNNING:
            shutil.rmtree(workdir, ignore_errors=True)


def _stage_source(workdir: Path, files: dict) -> None:
    for rel, content in files.items():
        target = workdir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            content = content.encode("utf-8")
        target.write_bytes(content or b"")


def _wait_healthy(container: str, port: int, timeout: int, cancelled) -> tuple[bool, str]:
    """Poll the container's health endpoint from within the shared network."""
    import time

    import requests

    url = f"http://{container}:{port}{HEALTH_PATH}"
    deadline = time.monotonic() + timeout
    last = "no response"
    base_restarts: Optional[int] = None
    while time.monotonic() < deadline:
        if cancelled():
            return False, "已取消"
        # Liveness + restart count in one inspect. A boot-crash under `--restart
        # unless-stopped` keeps the container "running" between relaunches, so the
        # plain State.Running==false check rarely fires and the crash masquerades as
        # a generic connection timeout. Catch the restart climb and name it.
        ins = _docker(["inspect", "-f", "{{.State.Running}} {{.RestartCount}}", container], 15)
        running, restarts = "true", 0
        if ins.returncode == 0:
            parts = ins.stdout.strip().split()
            running = parts[0] if parts else "true"
            try:
                restarts = int(parts[1]) if len(parts) > 1 else 0
            except ValueError:
                restarts = 0
        if running == "false":
            return False, "容器已退出(启动即崩溃)"
        if base_restarts is None:
            base_restarts = restarts
        elif restarts - base_restarts >= 2:
            return False, f"容器反复重启(启动即崩溃,RestartCount={restarts})"
        try:
            resp = requests.get(url, timeout=5)
            if resp.status_code < 500:
                return True, f"{resp.status_code} @ {HEALTH_PATH}"
            last = f"HTTP {resp.status_code}"
        except Exception as error:  # noqa: BLE001
            last = type(error).__name__
        time.sleep(3)
    return False, f"超时未就绪({last})"


def _smoke_endpoints(project_id: str) -> list[str]:
    """Sample idempotent, no-path-param GET paths from the shared contract.

    Returns ``[HEALTH_PATH, ...]`` with up to ``SMOKE_MAX_ENDPOINTS`` contract GET
    endpoints (those without a ``{param}`` segment). When the contract has no
    structured OpenAPI paths (deterministic fallback), only ``HEALTH_PATH`` is
    returned, so smoke degrades to a liveness re-check and never over-asserts.
    """
    from backend.services.code.fullstack import contract_service

    paths = [HEALTH_PATH]
    try:
        row = contract_service.get_ledger(project_id)
        contract = row.get_api_contract() if row else {}
        openapi = (contract or {}).get("openapi") or {}
        spec_paths = openapi.get("paths") if isinstance(openapi, dict) else None
        if isinstance(spec_paths, dict):
            for path, ops in spec_paths.items():
                if not isinstance(path, str) or "{" in path:  # skip path-param endpoints
                    continue
                if not isinstance(ops, dict) or "get" not in {str(k).lower() for k in ops}:
                    continue
                if path in paths:
                    continue
                paths.append(path)
                if len(paths) >= SMOKE_MAX_ENDPOINTS + 1:  # +1 for HEALTH_PATH
                    break
    except Exception:  # noqa: BLE001 — contract unreadable → smoke only /health
        logger.warning("smoke: could not read contract for %s", project_id, exc_info=True)
    return paths


def _smoke_test(container: str, port: int, project_id: str, cancelled) -> tuple[bool, list]:
    """Hit the sampled GET endpoints to confirm routes resolve (status < 500).

    Returns ``(ok, results)``. ``ok`` is False ONLY when an endpoint returns a
    definitive 5xx (route exists but the handler crashes / contract drift). A
    connection error / timeout is recorded as 'inconclusive' and does NOT fail the
    deploy — /health already proved liveness and a cold first hit on a DB-backed
    list endpoint can be slow; failing on that would roll back a healthy deploy.
    """
    import requests

    results: list[dict] = []
    ok = True
    for path in _smoke_endpoints(project_id):
        if cancelled():
            break
        url = f"http://{container}:{port}{path}"
        try:
            resp = requests.get(url, timeout=SMOKE_TIMEOUT)
            passed = resp.status_code < 500
            results.append({"endpoint": f"GET {path}", "status": resp.status_code,
                            "result": "ok" if passed else "5xx"})
            if not passed:
                ok = False
        except Exception as error:  # noqa: BLE001 — inconclusive, not a 5xx
            results.append({"endpoint": f"GET {path}", "result": f"inconclusive ({type(error).__name__})"})
    return ok, results


def _fail(
    dep: CodeDeployment,
    rollback: list,
    message: str,
    narrate: Optional[Callable[..., None]] = None,
) -> dict:
    # Narrate the rollback onto the deploy timeline so a failed deploy SHOWS its
    # recovery (tearing down container / image / database) instead of silently
    # jumping to a final error — previously the workflow's rollback render branch
    # never fired because the service rolled back without emitting any phase.
    rolled = bool(rollback)
    if rolled and narrate:
        narrate(
            "rollback",
            f"部署失败，正在回滚已创建的资源（{len(rollback)} 项：容器 / 镜像 / 数据库）…",
            {"actions": len(rollback)},
        )
    _run_rollback(rollback)
    if rolled and narrate:
        narrate("rollback", "已回滚到部署前状态")
    # Keep the DB status and the returned status in lockstep (callers read both):
    # ROLLED_BACK when we actually undid provisioned resources, otherwise a plain
    # FAILED (the deploy failed before anything was created — nothing to undo).
    status = DeploymentStatus.ROLLED_BACK if rolled else DeploymentStatus.FAILED
    try:
        dep.status = status
        dep.error_message = message
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
    return {"success": False, "status": status, "error": message}


def _abort(dep: CodeDeployment, rollback: list, message: str) -> dict:
    _run_rollback(rollback)
    try:
        dep.status = DeploymentStatus.STOPPED
        dep.error_message = message
        db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()
    return {"success": False, "status": DeploymentStatus.STOPPED, "error": message}


def _run_rollback(rollback: list) -> None:
    for action in reversed(rollback):
        try:
            action()
        except Exception:  # noqa: BLE001
            logger.warning("rollback action failed", exc_info=True)


def stop_deployment(project_id: str) -> bool:
    """Stop + remove a running deployment's container (keeps the db/image)."""
    dep = get_deployment(project_id)
    if not dep or not dep.container_name:
        return True
    _remove_container(dep.container_name)
    dep.status = DeploymentStatus.STOPPED
    db.session.commit()
    return True
