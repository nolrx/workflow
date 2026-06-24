"""
Atomic full-stack deploy service.

Brings a generated app up behind a reverse proxy in dependency order, with
rollback on any failure (so a half-deployed app never lingers):

    provision middleware namespace  (CREATE DATABASE app_<pid> + redis prefix)
      → apply generated init.sql      (best-effort fallback for non-self-migrating backends)
      → docker build the generated backend image (its OWN Dockerfile)
      → docker run a long-lived container on the shared app network
      → health-check  GET http://app-<pid>:<port>/health
      → contract smoke   (sampled GETs must not 5xx)
      → integration test (pull FE call-code + contract → AI plan → EXECUTE against
                          the live container; deterministic, tiered hard gate)
      → comprehensive Codex repair (on any smoke/itest defect: one Codex pass fixes
                          database + runtime 5xx + interface, rebuild + swap + re-check;
                          ≤ APP_DEPLOY_REPAIR_ROUNDS rounds; best-effort-proceed)
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
# First-screen visibility probe: after smoke, log in with the generated backend's
# mandated demo account and assert a core owner-filtered list is non-empty. This
# turns the silently-empty first screen ("进入系统后什么都没有") from an invisible
# success into a recorded signal. Purely advisory — never rolls a deploy back.
# Credentials MUST mirror the demo self-seed contract in backend_project_prompt.txt.
FIRST_SCREEN_PROBE = os.getenv("APP_FIRST_SCREEN_PROBE", "1") not in ("0", "false", "False", "")
FIRST_SCREEN_TIMEOUT = int(os.getenv("APP_FIRST_SCREEN_TIMEOUT", "15"))  # per-request, seconds
SEED_DEMO_EMAIL = os.getenv("APP_SEED_DEMO_EMAIL", "demo@example.com")
SEED_DEMO_PASSWORD = os.getenv("APP_SEED_DEMO_PASSWORD", "Demo1234!")
# Passwordless (phone / SMS / email-code / OTP) demo login. The deployed env has
# no real SMS/email channel, so the generated backend's demo mandate ships a fixed
# dev verification code for the demo identifier; the probe replays that exact path
# so SMS/OTP apps get a real seeded/empty signal instead of always 'inconclusive'.
SEED_DEMO_PHONE = os.getenv("APP_SEED_DEMO_PHONE", "13800000000")
SEED_DEMO_OTP = os.getenv("APP_SEED_DEMO_OTP", "000000")

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


def _run_container(container: str, image_tag: str, prov) -> subprocess.CompletedProcess:
    """(Re)start the backend container from ``image_tag`` on the shared network with
    the injected runtime env. Removes any existing container first so it is safe to
    call both for the initial start and to swap in a repaired image."""
    _remove_container(container)
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
    return _docker(run_args, 120)


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


def _load_zip_artifact(art: Optional[AgentArtifact]) -> dict:
    """Unzip a source-zip artifact into ``{rel: bytes}`` (empty on any problem)."""
    if not art or not art.storage_path:
        return {}
    abs_path = artifact_abs_path(art.storage_path)
    if not os.path.exists(abs_path):
        return {}
    files: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(abs_path, "r") as archive:
            for name in archive.namelist():
                if name.endswith("/"):
                    continue
                files[name] = archive.read(name)
    except Exception:  # noqa: BLE001 — corrupt/unreadable zip → caller falls back
        logger.warning("could not read source zip artifact %s", art.id, exc_info=True)
        return {}
    return files


def _latest_repaired_artifact(project_id: str, user_id: str) -> Optional[AgentArtifact]:
    """The most recent PROMOTED (deploy-repaired) backend source zip for a project,
    owned by ``user_id``. Promotion publishes it on the deploy run with this type."""
    return (
        AgentArtifact.query.filter_by(
            domain_ref_type="code_backend_project_repaired_zip", domain_ref_id=project_id
        )
        .join(AgentRun, AgentArtifact.run_id == AgentRun.id)
        .filter(AgentRun.user_id == user_id)
        .order_by(AgentArtifact.created_at.desc())
        .first()
    )


def _resolve_backend_source(project_id: str, user_id: str, backend_run: AgentRun) -> dict:
    """Backend source to deploy. Prefers the latest promoted (deploy-repaired) zip so
    a re-deploy builds from the FIXED code; falls back to the generation run's zip."""
    rep = _latest_repaired_artifact(project_id, user_id)
    if rep:
        files = _load_zip_artifact(rep)
        if files:
            return files
    return _load_backend_source(backend_run)


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
    run_id: Optional[str] = None,
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

    # Prefer the latest PROMOTED (deploy-repaired) backend source so a re-deploy
    # builds from the fixed code; fall back to the backend-generation run's zip.
    source = _resolve_backend_source(project.id, user_id, backend_run)
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
    # Auto-repair (best-effort) bookkeeping: the image actually running (may advance
    # to a repaired tag), how many itest repair rounds landed, and the agent's
    # per-round fix notes — used to PROMOTE the repaired source after a green deploy.
    running_image = image_tag
    itest_repaired_rounds = 0
    fix_notes: list[str] = []
    # The clean source snapshot that ``running_image`` was built from — tracked
    # through the itest repair ladder so PROMOTE only ever ships source that MATCHES
    # the running image (a failed round leaves the workdir dirty; we never promote
    # that). None until the ladder runs; then it advances only on a healthy round.
    good_source: Optional[dict] = None
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
        run = _run_container(container, image_tag, prov)  # clears any stale container first
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
                    # The recovery WIPED init.sql's tables (DB is now empty; the
                    # backend self-migrated). Reflect that locally so the downstream
                    # comprehensive-repair brief tells Codex the data layer is empty /
                    # init.sql is NOT in effect — not the stale "已应用" from line ~357.
                    init_applied = False
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

        # --- 6. Smoke test (contract liveness — non-blocking signal) ---------
        # /health proved the process is up; now hit a few idempotent no-param GET
        # endpoints to catch obvious route/contract drift. NON-BLOCKING: a 5xx here
        # is NOT a rollback — the deeper integration test (next) tests a superset and
        # will AUTO-REPAIR the backend, and per the best-effort-proceed policy a
        # residual interface defect ships with a warning rather than blocking the user.
        if cancelled():
            return _abort(dep, rollback, "已取消")
        phase("smoke", "契约冒烟:抽样调用幂等 GET 端点确认路由可达")
        smoke_ok, smoke_detail = _smoke_test(container, BACKEND_PORT, project.id, cancelled)
        dep.set_detail({**dep.get_detail(), "smoke": smoke_detail})
        db.session.commit()
        if not smoke_ok:
            phase("smoke", f"⚠ 契约冒烟发现 5xx,转入接口联调 + 自动修复处理:{smoke_detail}",
                  {"smoke": smoke_detail})
        else:
            phase("smoke", "契约冒烟通过(抽样端点路由可达)", {"smoke": smoke_detail})

        # --- 6a. Frontend↔backend integration test (defect detection) --------
        # Pull the generated FRONTEND's actual API-calling code + the shared contract,
        # distill a TARGETED test plan (which endpoints the frontend really calls and
        # which response fields it parses), then EXECUTE that plan against the live
        # container and DETERMINISTICALLY judge it. This (together with the smoke 5xx
        # signal above) is the DETECTION half; the repair half is the comprehensive
        # Codex ladder in 6a-bis (database + runtime + interface, one pass per round).
        # The frontend dist is NEVER touched (it was built against the contract). A
        # harness bug fails OPEN (a green deploy is never sunk by a detection error).
        if cancelled():
            return _abort(dep, rollback, "已取消")
        itest: dict = {}
        itest_plan = None
        try:
            from backend.services.code.fullstack import integration_test_service

            phase("itest", "前后端接口联调:拉取前端调用代码 + 共享契约,生成并执行全面接口测试")
            itest = integration_test_service.run_integration_tests(
                project_id=project.id, user_id=user_id, team_id=team_id,
                container=container, port=BACKEND_PORT, frontend_run=frontend_run,
                run_id=run_id, cancelled=cancelled,
            )
            itest_plan = itest.get("plan")
        except Exception:  # noqa: BLE001 — harness bug must NEVER sink a green deploy
            try:
                db.session.rollback()
            except Exception:  # noqa: BLE001
                pass
            logger.warning("integration test harness failed (fail-open)", exc_info=True)
            phase("itest", "接口联调步骤自身异常,已跳过(不阻断部署;health 已通过)")
            itest = {}
        if itest:
            dep.set_detail({**dep.get_detail(), "integration_test": itest.get("summary")})
            db.session.commit()

        # --- 6a-bis. Comprehensive Codex deploy-repair ladder (best-effort) --
        # Replaces the old contract-only Claude rung. A post-start defect from EITHER
        # detector (smoke 5xx OR an itest deterministic failure) enters a multi-round
        # ladder where EACH round is ONE thorough Codex repair of the running backend —
        # database / data-layer, runtime (5xx) crashes AND interface defects, fixed
        # together from an AGGREGATED brief (data-layer state + smoke 5xx + itest
        # failures + container stack traces + the shared contract). After each pass we
        # REBUILD ("再次自建") the image, swap the container, then run the SYNCHRONIZED
        # re-check (health + smoke + itest); a residual defect loops into the next round
        # (≤ APP_DEPLOY_REPAIR_ROUNDS). Codex-driven (the be-agent image pre-bakes the
        # OpenAI Codex CLI). Only the BACKEND is touched (the frontend dist was built
        # against the contract). Best-effort-proceed: an unfixable residual ships with a
        # warning — ONLY a repaired image that won't come up HEALTHY rolls anything back
        # (liveness is the sole hard gate here); a harness bug fails OPEN.
        max_repair_rounds = int(os.getenv("APP_DEPLOY_REPAIR_ROUNDS", "3"))
        post_start_defect = (itest.get("gate") == "fail") or (not smoke_ok)
        if max_repair_rounds > 0 and post_start_defect and not cancelled():
            try:
                from backend.services.code.fullstack import (
                    contract_service,
                    integration_test_service,
                )
                _row = contract_service.get_ledger(project.id)
                contract_block = integration_test_service._render_contract_block(
                    _row.get_api_contract() if _row else {}
                )
                # If the initial itest harness threw (itest_plan is None) but we still
                # entered the ladder via a smoke 5xx, seed a deterministic fallback plan
                # so the synchronized re-check REUSES it (no AI re-plan, no re-charge).
                if itest_plan is None:
                    itest_plan = integration_test_service._contract_fallback_plan(
                        _row.get_api_contract() if _row else {}
                    )
                # Snapshot the source the CURRENTLY-running image was built from
                # BEFORE any round dirties the workdir, so a failed round can never
                # cause us to promote source that doesn't match the running image.
                good_source = _collect_repaired_source(workdir, set(source.keys())) if workdir else None
                round_n = 0
                while (((itest.get("gate") == "fail") or (not smoke_ok))
                       and round_n < max_repair_rounds and not cancelled()):
                    round_n += 1
                    # Pull a generous log tail AFTER detection so the smoke/itest 5xx
                    # stack traces (runtime + DB errors) are in the brief.
                    logs = _docker(["logs", "--tail", "200", container], 30)
                    digest = _build_comprehensive_digest(
                        itest=itest, smoke_results=smoke_detail,
                        container_logs=(logs.stdout or logs.stderr or ""),
                        contract_block=contract_block, prov=prov, init_applied=init_applied,
                    )
                    phase("repair", f"启动 Codex 彻底修复(第 {round_n}/{max_repair_rounds} 轮,一次修完 数据库 + 运行报错 + 接口,只改后端、不动前端)")
                    rep = get_backend_project_service().repair_5xx(
                        workdir=str(workdir), failures_digest=digest,
                        on_log=lambda m: phase("repair", m), is_cancelled=cancelled,
                    )
                    if not rep.get("ran"):
                        phase("repair", "Codex 修复不可用(未配置 OPENAI_API_KEY),按尽力放行继续")
                        break
                    if rep.get("summary"):
                        fix_notes.append(rep["summary"])
                    new_tag = f"{image_tag}-fix{round_n}"
                    phase("repair", "彻底修复完成,重新自建镜像(docker build)")
                    build = _docker(["build", "-t", new_tag, str(workdir)], BUILD_TIMEOUT)
                    if build.returncode != 0:
                        phase("repair", "修复后镜像重建失败,保留修复前容器,停止修复(尽力放行)")
                        break
                    rollback.append(lambda t=new_tag: _docker(["image", "rm", "-f", t], 60))
                    # Swap the container to the repaired image; if it won't start /
                    # go healthy, restore the last-known-good image so repair never
                    # leaves the app worse than before.
                    swap = _run_container(container, new_tag, prov)
                    healthy = False
                    if swap.returncode == 0:
                        healthy, _ = _wait_healthy(container, BACKEND_PORT, HEALTH_TIMEOUT, cancelled)
                    if not healthy:
                        # Revert to the last-known-good image. Liveness is a HARD gate
                        # (best-effort-proceed covers interface grounds, NOT "is the app
                        # up"): _run_container removed the good container to swap, so if
                        # the revert can't bring a HEALTHY one back, roll the whole
                        # deploy back rather than register a dead container as RUNNING.
                        rb = _run_container(container, running_image, prov)
                        reverted = rb.returncode == 0 and _wait_healthy(
                            container, BACKEND_PORT, HEALTH_TIMEOUT, cancelled)[0]
                        if not reverted:
                            return _fail(
                                dep, rollback,
                                "修复后容器无法启动,且回滚到修复前镜像也未能恢复健康",
                                narrate=phase,
                            )
                        phase("repair", "修复后容器无法启动/健康检查未过,已恢复修复前镜像,停止修复(尽力放行)")
                        break
                    running_image = new_tag
                    itest_repaired_rounds = round_n
                    # This round built + swapped + passed health → the workdir matches
                    # the new running image; advance the promotable snapshot.
                    good_source = _collect_repaired_source(workdir, set(source.keys())) if workdir else good_source
                    # --- SYNCHRONIZED re-check: smoke + itest against the fixed image -
                    phase("repair", "同步复检:契约冒烟 + 前后端接口联调")
                    smoke_ok, smoke_detail = _smoke_test(container, BACKEND_PORT, project.id, cancelled)
                    itest = integration_test_service.run_integration_tests(
                        project_id=project.id, user_id=user_id, team_id=team_id,
                        container=container, port=BACKEND_PORT, frontend_run=frontend_run,
                        run_id=run_id, cancelled=cancelled, plan=itest_plan,
                    )
                    itest_plan = itest.get("plan") or itest_plan
                    dep.set_detail({**dep.get_detail(),
                                    "smoke": smoke_detail,
                                    "integration_test": itest.get("summary")})
                    db.session.commit()
                if itest_repaired_rounds and (itest.get("gate") != "fail") and smoke_ok:
                    phase("repair", f"Codex 彻底修复后复检通过(仅改后端,修复 {itest_repaired_rounds} 轮)",
                          {"integration_test": itest.get("summary")})
            except Exception:  # noqa: BLE001 — repair harness bug must not sink the deploy
                try:
                    db.session.rollback()
                except Exception:  # noqa: BLE001
                    pass
                logger.warning("comprehensive deploy-repair ladder failed (fail-open)", exc_info=True)
                phase("repair", "Codex 修复过程异常,已跳过(尽力放行)")

        # Record whichever image is actually running now (a repaired tag, if any).
        if running_image != image_tag:
            dep.image_tag = running_image
            db.session.commit()

        # Terminal: BEST-EFFORT PROCEED (尽力放行) — never roll back on itest grounds.
        if itest.get("gate") == "fail":
            phase("itest",
                  f"⚠ 接口联调仍有未修复的确定性失败,按尽力放行策略继续部署(残留降为告警):{itest.get('reason')}",
                  {"integration_test": itest.get("summary")})
        elif itest:  # ran cleanly → narrate the pass / warn / inconclusive outcome
            summary = itest.get("summary") or {}
            warns = summary.get("warnings") or []
            executed = summary.get("executed", 0)
            if itest.get("gate") == "inconclusive":
                phase("itest", f"接口联调未下定论(不阻断部署):{itest.get('reason') or '无可执行的契约端点'}",
                      {"integration_test": summary})
            elif warns:
                phase("itest", f"接口联调通过(执行 {executed} 项,{len(warns)} 项告警,详见部署详情)",
                      {"integration_test": summary})
            else:
                phase("itest", f"接口联调通过(执行 {executed} 项关键接口,响应结构符合前端解析预期)",
                      {"integration_test": summary})

        # Honest residual signal: a still-red smoke (definitive 5xx) after the repair
        # ladder is downgraded to a warning, not a rollback (best-effort-proceed).
        if not smoke_ok:
            phase("repair", "⚠ 契约冒烟仍有 5xx 残留,按尽力放行策略继续部署(已记录,建议人工排查)",
                  {"smoke": smoke_detail})

        # --- 6b. First-screen visibility (advisory) --------------------------
        # /health + smoke prove the app is up and routes resolve, but a list
        # endpoint returning an empty 200 is indistinguishable from success —
        # exactly the "进入系统后什么都没有" the generated backend's demo self-seed
        # mandate fixes. Log in with the seeded demo account and assert a core list
        # is non-empty. Purely advisory: it records the signal + narrates a warning
        # when empty, but NEVER rolls back (a cold/slow list or an unguessable login
        # shape must not sink a /health-green deploy). Fully isolated so a probe bug
        # can never reach the deploy's rollback path.
        if FIRST_SCREEN_PROBE:
            try:
                if not cancelled():  # cancel check inside the try → can't reach rollback
                    phase("verify", "首屏校验:用 demo 账号登录并抽查核心列表是否非空(建议性,不回滚)")
                    fs = _first_screen_probe(container, BACKEND_PORT, project.id, cancelled)
                    dep.set_detail({**dep.get_detail(), "first_screen": fs})
                    db.session.commit()
                    state = fs.get("state")
                    if state == "empty":
                        phase("verify",
                              f"⚠ 首屏疑似为空:{fs.get('detail')};部署仍继续——请检查生成后端的 "
                              "demo self-seed(SEED_DEMO_DATA 默认开)是否生效", {"first_screen": fs})
                    elif state == "seeded":
                        phase("verify", f"首屏校验通过:{fs.get('detail')}", {"first_screen": fs})
                    else:
                        phase("verify", f"首屏校验未下定论(不影响部署):{fs.get('detail')}", {"first_screen": fs})
            except Exception:  # noqa: BLE001 — advisory only; must NEVER affect deploy outcome
                # Critical: roll the session back so a failed probe commit can't
                # leave it in a PendingRollback state that would make the
                # downstream RUNNING-commit throw and sink a health-green deploy.
                try:
                    db.session.rollback()
                except Exception:  # noqa: BLE001
                    pass
                logger.warning("first-screen probe failed (advisory)", exc_info=True)

        # A cancel landing mid-smoke leaves _smoke_test returning ok=True (it just
        # breaks the loop), so re-check here before committing the deploy as RUNNING
        # — otherwise a late cancel would be swallowed and the app registered anyway.
        if cancelled():
            return _abort(dep, rollback, "已取消")

        # --- 6c. PROMOTE the repaired source ---------------------------------
        # Publish the repaired backend source so download / re-deploy use the fixed
        # code ("logical overwrite, physical additive"). It must EXACTLY match the
        # running image: when the comprehensive Codex repair ladder ran, use
        # ``good_source`` (the snapshot tracked to the last HEALTHY round — a failed
        # round's dirty workdir is never promoted); otherwise (build-repair only, no
        # repair ladder) the workdir is clean and matches the image, so collect it.
        # Only the BACKEND is repaired; the frontend dist is untouched.
        build_rounds = int(dep.get_detail().get("build_repaired_rounds") or 0)
        repaired_source: dict = {}
        fix_summary: Optional[dict] = None
        collected = good_source
        if collected is None and build_rounds and workdir and workdir.exists():
            collected = _collect_repaired_source(workdir, set(source.keys()))
        # Promote only if the source actually changed (edits / additions / deletions)
        # — a repair round that produced byte-identical output is not promoted, and
        # `collected` empty / None (workdir vanished, or nothing ran) never promotes.
        if collected and collected != source:
            changed = sorted(rel for rel in collected if collected.get(rel) != source.get(rel))
            removed = sorted(set(source) - set(collected))
            repaired_source = collected
            # ``itest_repaired_rounds`` now counts comprehensive Codex repair rounds
            # (kept under the old key for downstream/back-compat); ``repair_engine``
            # records which post-start engine ran when any round landed.
            fix_summary = {
                "build_repaired_rounds": build_rounds,
                "itest_repaired_rounds": itest_repaired_rounds,
                "repair_engine": "codex" if itest_repaired_rounds else None,
                "changed_files": changed,
                "removed_files": removed,
                "notes": [n for n in fix_notes if n],
            }
            dep.set_detail({**dep.get_detail(), "promoted_fix": {
                "build_repaired_rounds": build_rounds,
                "itest_repaired_rounds": itest_repaired_rounds,
                "repair_engine": "codex" if itest_repaired_rounds else None,
                "changed_files": changed,
                "removed_files": removed,
            }})
            db.session.commit()

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
            "image": running_image,
            "api_base": dep.api_base_path,
            "preview_url": f"/preview/{project.id}/",
            "middleware": prov.to_dict(),
            # Promote the repaired backend source to the user (workflow publishes it).
            "repaired": bool(fix_summary),
            "repaired_source": repaired_source,
            "fix_summary": fix_summary,
        }
    except subprocess.TimeoutExpired as error:
        return _fail(dep, rollback, f"部署超时:{error}", narrate=phase)
    except Exception as error:  # noqa: BLE001
        logger.error("deploy failed for %s: %s", project.id, error, exc_info=True)
        return _fail(dep, rollback, f"部署异常:{error}", narrate=phase)
    finally:
        if workdir and workdir.exists() and dep.status != DeploymentStatus.RUNNING:
            shutil.rmtree(workdir, ignore_errors=True)


# Build-output dirs / files the in-container native build (npm ci / mvn package /
# pip install …) may leave in the workdir — excluded when collecting the repaired
# SOURCE so the promoted zip stays source-only (mirrors the original artifact).
_REPAIR_EXCLUDE_DIRS = {
    "node_modules", "target", "build", "dist", "out", ".git", "__pycache__",
    ".venv", "venv", ".gradle", ".next", ".nuxt", "coverage", ".pytest_cache",
    "bin", "obj", ".idea", ".vscode", ".mypy_cache", ".ruff_cache",
}
_REPAIR_EXCLUDE_SUFFIX = (".class", ".pyc", ".pyo", ".o", ".a", ".so", ".log")
_MAX_REPAIR_FILE = int(os.getenv("APP_REPAIR_MAX_FILE", str(2 * 1024 * 1024)))  # 2MB/file


def _is_excluded_path(rel: str) -> bool:
    if set(Path(rel).parts) & _REPAIR_EXCLUDE_DIRS:
        return True
    return rel.endswith(_REPAIR_EXCLUDE_SUFFIX)


def _collect_repaired_source(workdir: Path, original_rels: set) -> dict:
    """Collect the (clean) backend source from a possibly-repaired workdir.

    Always includes every ORIGINAL file that still exists (guaranteed source, even
    if it lives under a name the exclude list would otherwise drop), then adds any
    NEW source files the agent created — filtering out build artifacts the
    in-container native build may have produced, and oversized files."""
    files: dict[str, bytes] = {}
    for rel in original_rels:
        p = workdir / rel
        if p.is_file():
            try:
                files[rel] = p.read_bytes()
            except OSError:
                continue
    for p in workdir.rglob("*"):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(workdir).as_posix()
        except ValueError:
            continue
        if rel in files or _is_excluded_path(rel):
            continue
        try:
            if p.stat().st_size > _MAX_REPAIR_FILE:
                continue
            files[rel] = p.read_bytes()
        except OSError:
            continue
    return files


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


def _build_comprehensive_digest(
    *, itest: dict, smoke_results: list, container_logs: str,
    contract_block: str, prov, init_applied: bool,
) -> str:
    """Aggregate EVERY observed post-start defect into one brief for the Codex
    comprehensive-repair pass: the data-layer state, the smoke 5xx endpoints, the
    itest deterministic failures, the container stack traces (runtime + DB errors)
    and the shared contract. One brief → one Codex pass fixes database + runtime +
    interface together. The itest-failures + logs + contract tail reuses the
    canonical formatter so the wording matches what the repair prompt expects."""
    from backend.services.code.fullstack import integration_test_service

    parts: list[str] = []
    # Data-layer state (lets Codex tell schema drift / empty-DB from app bugs).
    dl = [f"- 中间件引擎:{getattr(prov, 'engine_kind', '?')}"]
    if getattr(prov, "db_name", None):
        dl.append(f"- 数据库:{prov.db_name}(部署环境真实库,可能为空 — 后端须能在空库上自建表)")
    dl.append(f"- 启动时已应用生成的 init.sql 兜底:{'是' if init_applied else '否(依赖后端自迁移)'}")
    parts.append("## 运行时 / 数据层状态\n" + "\n".join(dl))
    # Smoke 5xx endpoints (route exists, handler crashes).
    fivexx = [r for r in (smoke_results or [])
              if isinstance(r, dict) and r.get("result") == "5xx"]
    if fivexx:
        parts.append(
            "## 契约冒烟发现的 5xx 端点(路由存在但 handler 崩溃)\n"
            + "\n".join(f"- {r.get('endpoint')} → HTTP {r.get('status')}" for r in fivexx)
        )
    # itest failures + container logs (stack traces) + contract — the canonical
    # formatter already appends the logs + contract sections, so feed once.
    parts.append(integration_test_service.format_failures_for_repair(
        itest or {}, contract_block=contract_block, logs=container_logs or "",
    ))
    return "\n\n".join(parts)


# --- first-screen visibility probe (advisory) --------------------------------
def _find_token(obj, depth: int = 0):
    """Recursively pull a bearer-ish token out of a login response JSON."""
    if depth > 4 or obj is None:
        return None
    if isinstance(obj, dict):
        for key in ("access_token", "accessToken", "token", "jwt", "id_token", "idToken", "accessJwt"):
            v = obj.get(key)
            if isinstance(v, str) and v:
                return v
        for v in obj.values():
            found = _find_token(v, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_token(v, depth + 1)
            if found:
                return found
    return None


def _classify_list_payload(payload) -> str:
    """Classify a GET body for the first-screen probe.

    Returns ``'data'`` (a non-empty collection), ``'empty'`` (a list-shaped but
    empty collection), or ``'unknown'`` (not a recognizable collection — a single
    object / scalar like ``GET /me`` or ``{"version":"1.0"}``). The probe uses
    this to bias toward 'inconclusive' rather than a false 'empty' warning on
    endpoints that simply aren't list endpoints. Handles a bare array, the common
    pagination envelopes (optionally nested), and a positive ``total``/``count``
    (authoritative: ``{"data":[],"total":5}`` is 'data', not 'empty').
    """
    if isinstance(payload, list):
        return "data" if payload else "empty"
    if isinstance(payload, dict):
        for key in ("total", "count", "totalCount", "total_count"):
            v = payload.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                return "data"
        saw_collection = False
        for key in ("data", "items", "results", "records", "list", "rows", "content"):
            v = payload.get(key)
            if isinstance(v, list):
                if v:
                    return "data"
                saw_collection = True
            elif isinstance(v, dict):
                inner = _classify_list_payload(v)
                if inner == "data":
                    return "data"
                if inner == "empty":
                    saw_collection = True
        return "empty" if saw_collection else "unknown"
    return "unknown"


def _looks_non_empty(payload) -> bool:
    """True when a GET response carries at least one business record."""
    return _classify_list_payload(payload) == "data"


def _login_endpoints(project_id: str) -> list[str]:
    """Contract POST paths (no path params) that look like an auth/login entry."""
    from backend.services.code.fullstack import contract_service

    scored: list[tuple[int, str]] = []
    try:
        row = contract_service.get_ledger(project_id)
        contract = row.get_api_contract() if row else {}
        spec_paths = ((contract or {}).get("openapi") or {}).get("paths") or {}
        if isinstance(spec_paths, dict):
            for path, ops in spec_paths.items():
                if not isinstance(path, str) or "{" in path or not isinstance(ops, dict):
                    continue
                if "post" not in {str(k).lower() for k in ops}:
                    continue
                low = path.lower()
                if not any(h in low for h in ("login", "signin", "sign-in", "session", "token", "authenticate")):
                    continue
                score = 0 if "login" in low else (1 if ("signin" in low or "sign-in" in low) else 2)
                scored.append((score, path))
    except Exception:  # noqa: BLE001 — advisory; unreadable contract → no login probe
        logger.warning("first-screen: could not read contract for %s", project_id, exc_info=True)
        return []
    # Cap the fan-out: each candidate costs up to 3 credential attempts ×
    # FIRST_SCREEN_TIMEOUT, so an unbounded list of 'token'/'session'-ish POST
    # paths could add minutes to every deploy. Keep the 3 most login-ish.
    return [p for _, p in sorted(scored)][:3]


def _otp_request_endpoints(project_id: str) -> list[str]:
    """Contract POST paths (no path params) that look like 'send a verification code'.

    For passwordless logins (phone/SMS/email-code/OTP) the app first POSTs here to
    request a code, then logs in with it. The deployed env has no real SMS/email
    channel, so this request is fire-and-forget — the demo account's fixed dev code
    works regardless; we still hit it in case the app gates login on a prior request.
    """
    from backend.services.code.fullstack import contract_service

    paths: list[str] = []
    try:
        row = contract_service.get_ledger(project_id)
        contract = row.get_api_contract() if row else {}
        spec_paths = ((contract or {}).get("openapi") or {}).get("paths") or {}
        if isinstance(spec_paths, dict):
            for path, ops in spec_paths.items():
                if not isinstance(path, str) or "{" in path or not isinstance(ops, dict):
                    continue
                if "post" not in {str(k).lower() for k in ops}:
                    continue
                low = path.lower()
                if any(h in low for h in ("login", "signin", "sign-in", "authenticate")):
                    continue  # that's the login itself, not the code-request
                if any(h in low for h in ("sms", "otp", "code", "captcha", "verif", "/send")):
                    paths.append(path)
    except Exception:  # noqa: BLE001 — advisory; unreadable contract → no otp probe
        return []
    return paths[:2]  # bound the fan-out, like _login_endpoints


def _first_screen_probe(container: str, port: int, project_id: str, cancelled) -> dict:
    """Advisory check that a freshly-deployed app's first screen has data.

    Samples owner-filtered GET list endpoints (anonymously first, then logged in
    with the mandated demo account) and asserts at least one returns a non-empty
    payload. Returns ``{state: seeded|empty|inconclusive, detail}``. Purely
    advisory: any failure resolves to 'inconclusive' and the deploy proceeds
    unchanged — this NEVER rolls a deploy back. It exists to surface the silently-
    empty first screen ('进入系统后什么都没有') that /health + smoke can't see.
    """
    import requests

    base = f"http://{container}:{port}"
    list_paths = [p for p in _smoke_endpoints(project_id) if p != HEALTH_PATH]
    if not list_paths:
        return {"state": "inconclusive", "detail": "契约无可抽样的列表端点"}

    def _sample(headers: dict):
        """Tri-state over the sampled endpoints:

        ``True``  — at least one returned a non-empty collection (data present).
        ``False`` — a list-shaped response was seen and ALL such were empty.
        ``None``  — inconclusive: no list-shaped response was readable (only
                    object/scalar endpoints, all unreachable), or a cancel landed.

        Biasing object/scalar-only responses to None (not False) avoids a false
        '首屏为空' warning on apps whose sampled no-param GETs aren't collections.
        """
        saw_empty_list = False
        for path in list_paths:
            if cancelled():
                return None  # don't record a partial 'empty' for a cancelled run
            try:
                resp = requests.get(f"{base}{path}", headers=headers, timeout=FIRST_SCREEN_TIMEOUT)
            except Exception:  # noqa: BLE001 — unreachable endpoint, skip
                continue
            if resp.status_code >= 400:
                continue
            try:
                cls = _classify_list_payload(resp.json())
            except ValueError:  # non-JSON body — can't judge
                continue
            if cls == "data":
                return True
            if cls == "empty":
                saw_empty_list = True
        return False if saw_empty_list else None

    # 1) Anonymous first — no-auth apps expose their data directly.
    anon = _sample({})
    if anon is True:
        return {"state": "seeded", "detail": "匿名抽样命中非空列表"}

    # 2) Authenticated — log in with the seeded demo account, retry with the token.
    token, login_tried = None, False
    for path in _login_endpoints(project_id):
        if cancelled() or token:
            break
        login_tried = True
        for body in (
            {"email": SEED_DEMO_EMAIL, "password": SEED_DEMO_PASSWORD},
            {"username": SEED_DEMO_EMAIL, "password": SEED_DEMO_PASSWORD},
            {"username": SEED_DEMO_EMAIL.split("@")[0], "password": SEED_DEMO_PASSWORD},
        ):
            try:
                resp = requests.post(f"{base}{path}", json=body, timeout=FIRST_SCREEN_TIMEOUT)
            except Exception:  # noqa: BLE001
                continue
            if resp.status_code >= 400:
                continue
            try:
                token = _find_token(resp.json())
            except ValueError:
                token = None
            if token:
                break

    # 2b) Passwordless / OTP fallback — apps that log in by phone/email verification
    # code. No real SMS/email channel in the deployed env, so we replay the demo
    # mandate's fixed dev code (SEED_DEMO_OTP) for the demo identifier: best-effort
    # request a code (ignored if it 4xxs — the fixed code works anyway), then submit
    # it against each login endpoint. Only runs when password login yielded nothing.
    if not token and not cancelled():
        for path in _otp_request_endpoints(project_id):
            for body in ({"phone": SEED_DEMO_PHONE}, {"mobile": SEED_DEMO_PHONE},
                         {"phone_number": SEED_DEMO_PHONE}, {"email": SEED_DEMO_EMAIL}):
                try:
                    requests.post(f"{base}{path}", json=body, timeout=FIRST_SCREEN_TIMEOUT)
                except Exception:  # noqa: BLE001 — fire-and-forget code request
                    pass
        for path in _login_endpoints(project_id):
            if cancelled() or token:
                break
            login_tried = True
            for body in (
                {"phone": SEED_DEMO_PHONE, "code": SEED_DEMO_OTP},
                {"mobile": SEED_DEMO_PHONE, "code": SEED_DEMO_OTP},
                {"phone_number": SEED_DEMO_PHONE, "code": SEED_DEMO_OTP},
                {"phone": SEED_DEMO_PHONE, "otp": SEED_DEMO_OTP},
                {"email": SEED_DEMO_EMAIL, "code": SEED_DEMO_OTP},
            ):
                try:
                    resp = requests.post(f"{base}{path}", json=body, timeout=FIRST_SCREEN_TIMEOUT)
                except Exception:  # noqa: BLE001
                    continue
                if resp.status_code >= 400:
                    continue
                try:
                    token = _find_token(resp.json())
                except ValueError:
                    token = None
                if token:
                    break

    if token:
        auth = _sample({"Authorization": f"Bearer {token}"})
        if auth is True:
            return {"state": "seeded", "detail": "demo 账号登录后抽样命中非空列表"}
        if auth is False:
            return {"state": "empty", "detail": "demo 账号登录成功但核心列表为空(疑似 demo self-seed 未生效)"}
        return {"state": "inconclusive", "detail": "demo 账号登录成功但列表端点不可读"}

    if not login_tried:
        if anon is False:
            return {"state": "empty", "detail": "无鉴权应用,匿名核心列表为空(疑似 demo self-seed 未生效)"}
        return {"state": "inconclusive", "detail": "无登录端点且列表端点不可读"}
    return {"state": "inconclusive",
            "detail": "demo 账号登录失败(密码与验证码/OTP 两种方式均未拿到 token——"
                      "凭据/哈希口径不符,或验证码登录缺固定开发验证码),无法判定首屏"}


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
