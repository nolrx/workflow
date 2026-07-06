"""
Backend Dev Mode container lifecycle — the long-running interactive BACKEND sandbox.

This is the backend-lane twin of ``dev_service`` (which runs the frontend Vite dev
server). It runs a LONG-RUNNING container that serves the GENERATED backend in its
NATIVE hot-reload mode (``uvicorn --reload`` / ``nodemon`` / ``flask --debug`` / Go
rebuild / ``mvn spring-boot:run``) instead of a built production image, so a
``docker exec claude -p`` edit round is reflected in seconds — the backend analogue
of Vite HMR. Deployment (``deploy_service``) stays the "graduate to production"
path (``docker build`` → ``app-<pid>``); dev is the fast interactive workbench.

Why NOT the frontend dev container: a container has ONE main process, the frontend
one IS ``npm run dev``, and the frontend image is Node-only. The backend is polyglot
(Python/Go/Java/Node) and needs a database, so it gets its OWN container
(``dev-be-<pid>``) on the shared network, from the ``be-agent`` image (full
toolchains), with an isolated dev database namespace.

Design mirrors ``dev_service`` (the adversarial-review must-fix items apply equally):
  * the gateway retry-proxy is started in the ENTRYPOINT and every ``docker exec``
    overrides ``ANTHROPIC_BASE_URL`` to it (reliable exec'd claude calls);
  * cancel kills the in-container claude (``pkill``); turns are serialized;
  * source lives in the container fs (seeded read-only once, guarded by ``.seeded``)
    so a ``docker restart`` keeps the agent's edits — only ``rm`` (stop) destroys it;
  * the run command is chosen per detected stack, but a project-provided
    ``dev-start.sh`` ALWAYS wins (the coding agent normalises its own runnable dev
    entry — the reliable way to cover a polyglot fleet).

Comments in English to match the Code/core convention.
"""
import json
import logging
import os
import subprocess
import tarfile
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Callable, Optional

from backend.services.code import middleware_service
from backend.services.code.docker_env import (
    ANTHROPIC_RETRY_PROXY_BOOTSTRAP,
    anthropic_agent_credentials,
    anthropic_configured,
    container_user,
    host_workdir,
)

logger = logging.getLogger(__name__)

DOCKER_BIN = os.getenv("DOCKER_BIN", "docker")
APP_NETWORK = os.getenv("APP_NETWORK", "worksflow-net")
# The be-agent image carries the polyglot toolchains (JDK+Maven / Go / Python / Node)
# needed to install deps + run any generated backend in dev; fall back to fe-agent.
DEV_BE_IMAGE = os.getenv("BE_AGENT_IMAGE", os.getenv("FE_AGENT_IMAGE", "fe-agent:latest"))
# The container-internal port the backend must listen on (matches deploy's BACKEND_PORT
# so the frontend's injected API base + itest harness are identical across dev/deploy).
DEV_BE_PORT = int(os.getenv("DEV_MODE_BE_PORT", os.getenv("APP_BACKEND_PORT", "8080")))
DEV_BE_MEM = os.getenv("DEV_MODE_BE_CONTAINER_MEM", "1024m")
DEV_BE_START_TIMEOUT = int(os.getenv("DEV_MODE_BE_START_TIMEOUT", "180"))
DEV_BE_INSTALL_TIMEOUT = int(os.getenv("DEV_MODE_BE_INSTALL_TIMEOUT", "600"))
DEV_BE_TURN_TIMEOUT = int(os.getenv("DEV_MODE_BE_TURN_TIMEOUT", "0"))

_MAX_FILE_BYTES = int(os.getenv("DEV_MODE_MAX_FILE_BYTES", str(2_000_000)))
_MAX_TOTAL_BYTES = int(os.getenv("DEV_MODE_MAX_TOTAL_BYTES", str(48_000_000)))
_COLLECT_EXCLUDE = ("node_modules", ".git", ".cache", ".npm", "dist", "target", "vendor", "__pycache__", ".venv")

# Headless claude flags — identical to the frontend dev container so behaviour matches.
_CLAUDE_FLAGS = (
    "--output-format stream-json --verbose "
    "--permission-mode bypassPermissions --allowedTools Read Write Edit Bash Skill"
)

_WORK = "/tmp/work"
_SEED = "/tmp/seed"


def _dev_be_container_name(project_id: str) -> str:
    """Per-project backend dev container name (distinct from dev-<pid> and app-<pid>)."""
    slug = middleware_service._sanitized_db_name(project_id)[4:]
    return f"dev-be-{slug}"[:60] or f"dev-be-{project_id[:12]}"


def _dev_db_key(project_id: str) -> str:
    """Provision key for the ISOLATED dev database namespace.

    Distinct from the deploy namespace (``app_<hex>``) so dev experimentation never
    reads/writes a live deployed app's data. ``provision_namespace`` sanitises this
    into ``app_<hex>dev`` (+ its own redis prefix)."""
    return f"{project_id}dev"


def _docker(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run([DOCKER_BIN, *args], capture_output=True, text=True, timeout=timeout)


def _ensure_network() -> None:
    try:
        if _docker(["network", "inspect", APP_NETWORK], 15).returncode != 0:
            _docker(["network", "create", APP_NETWORK], 30)
    except Exception:  # noqa: BLE001
        logger.warning("could not ensure app network %s", APP_NETWORK)


def docker_available() -> bool:
    try:
        return _docker(["version", "--format", "{{.Server.Version}}"], 15).returncode == 0
    except Exception:  # noqa: BLE001
        return False


# A minimal, runnable Node + Express backend so the dev container ALWAYS has
# something serving on $PORT with a /health probe (mirrors the frontend's minimal
# Vite scaffold). Used only when a project has no prior backend source; the first
# turn then implements the real API per the contract.
_MINIMAL_BACKEND_SCAFFOLD: dict[str, bytes] = {
    "package.json": json.dumps({
        "name": "dev-backend",
        "private": True,
        "version": "0.0.0",
        "scripts": {"dev": "node --watch server.js", "start": "node server.js"},
        "dependencies": {"express": "^4.19.2"},
    }, indent=2).encode("utf-8"),
    "server.js": (
        "const express = require('express');\n"
        "const app = express();\n"
        "app.use(express.json());\n"
        "app.get('/health', (req, res) => res.json({ status: 'ok' }));\n"
        "const port = process.env.PORT || 8080;\n"
        "app.listen(port, '0.0.0.0', () => console.log('[dev-backend] listening on ' + port));\n"
    ).encode("utf-8"),
    "dev-start.sh": (
        "#!/usr/bin/env bash\n"
        "set -e\n"
        "npm install --no-audit --no-fund\n"
        "exec npm run dev\n"
    ).encode("utf-8"),
}


# The container entrypoint (container main process). Detects the stack, installs
# deps, and starts the backend in hot-reload mode. A project-provided ``dev-start.sh``
# ALWAYS wins — the coding agent is instructed to write one (the reliable way to run
# any polyglot backend). ${PORT} comes from ``docker run -e``. The gateway retry-proxy
# bootstrap is spliced in so exec'd claude calls reach a live proxy.
_DEV_BE_ENTRYPOINT = r"""
set -u
export HOME="${HOME:-/home/node}"
export PATH="$HOME/bin:$HOME/.local/bin:$PATH"
export PORT="${PORT:-8080}"
mkdir -p "$HOME/bin" "$HOME/.claude/skills/image-assets" "$HOME/.codex" 2>/dev/null || true

# __ANTHROPIC_PROXY_BOOTSTRAP__

# Seed the workspace ONCE from the read-only /seed mount (keeps agent edits on restart).
mkdir -p "%WORK%"
if [ ! -f "%WORK%/.seeded" ]; then
  if [ -d "%SEED%" ] && [ -n "$(ls -A %SEED% 2>/dev/null)" ]; then
    cp -a %SEED%/. "%WORK%"/ 2>/dev/null || true
  fi
  touch "%WORK%/.seeded"
fi
cd "%WORK%"

# Locate the project root (dir carrying a stack manifest), tolerate one level of nesting.
ROOT="%WORK%"
if [ ! -f "$ROOT/package.json" ] && [ ! -f "$ROOT/requirements.txt" ] && [ ! -f "$ROOT/pyproject.toml" ] \
   && [ ! -f "$ROOT/go.mod" ] && [ ! -f "$ROOT/pom.xml" ] && [ ! -f "$ROOT/dev-start.sh" ]; then
  found="$(find "%WORK%" -maxdepth 3 \( -name dev-start.sh -o -name package.json -o -name requirements.txt \
           -o -name pyproject.toml -o -name go.mod -o -name pom.xml \) \
           -not -path '*/node_modules/*' 2>/dev/null | head -n1)"
  [ -n "$found" ] && ROOT="$(dirname "$found")"
fi
cd "$ROOT"
echo "$ROOT" > /tmp/dev_project_root

# Git baseline (fail-soft; used by any future parallel-lane worktrees).
if command -v git >/dev/null 2>&1; then
  if ! git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    printf 'node_modules/\ntarget/\n.venv/\nvendor/\n__pycache__/\n*.log\n' > "$ROOT/.gitignore"
    git -C "$ROOT" init -q >/dev/null 2>&1 || true
    git -C "$ROOT" config user.email agent@worksflow.local >/dev/null 2>&1 || true
    git -C "$ROOT" config user.name worksflow-agent >/dev/null 2>&1 || true
  fi
  git -C "$ROOT" add -A >/dev/null 2>&1 || true
  git -C "$ROOT" commit -q -m dev-be-base --allow-empty >/dev/null 2>&1 || true
fi

start_backend() {
  cd "$ROOT"
  # A project-provided dev entry always wins (the agent normalises its own runner).
  if [ -f "$ROOT/dev-start.sh" ]; then
    echo "[dev-be] running project dev-start.sh" >&2
    exec bash "$ROOT/dev-start.sh"
  fi

  # Node ------------------------------------------------------------------------
  if [ -f "$ROOT/package.json" ]; then
    echo "[dev-be] node: npm install" >&2
    npm install --no-audit --no-fund 2>&1 | tail -n 100 >&2 || true
    if npm run 2>/dev/null | grep -qE '^\s*dev\b'; then exec npm run dev; fi
    ENTRY="$(node -e "try{process.stdout.write(require('./package.json').main||'')}catch(e){}" 2>/dev/null)"
    if [ -z "$ENTRY" ]; then
      for c in server.js index.js app.js src/index.js src/server.js src/app.js dist/index.js dist/server.js; do
        [ -f "$c" ] && ENTRY="$c" && break
      done
    fi
    if [ -n "$ENTRY" ]; then exec npx --yes nodemon --watch . -e js,cjs,mjs,ts,json --exec "node $ENTRY"; fi
    exec npm start
  fi

  # Python ----------------------------------------------------------------------
  if [ -f "$ROOT/requirements.txt" ] || [ -f "$ROOT/pyproject.toml" ]; then
    echo "[dev-be] python: installing deps" >&2
    [ -f requirements.txt ] && pip install --no-input -r requirements.txt 2>&1 | tail -n 80 >&2 || true
    [ -f pyproject.toml ] && pip install --no-input -e . 2>&1 | tail -n 40 >&2 || true
    pip install --no-input uvicorn 2>/dev/null >&2 || true
    ASGI="$(grep -rlE 'FastAPI\(|Starlette\(' --include=*.py . 2>/dev/null | head -n1)"
    if [ -n "$ASGI" ]; then
      MOD="$(printf '%s' "$ASGI" | sed 's#^\./##; s#/#.#g; s#\.py$##')"
      VAR="$(grep -oE '^[a-zA-Z_][a-zA-Z0-9_]* *= *FastAPI\(|^[a-zA-Z_][a-zA-Z0-9_]* *= *Starlette\(' "$ASGI" | head -n1 | sed 's/ *=.*//')"
      [ -z "$VAR" ] && VAR=app
      echo "[dev-be] uvicorn $MOD:$VAR --reload" >&2
      exec uvicorn "$MOD:$VAR" --reload --host 0.0.0.0 --port "$PORT"
    fi
    FLASKF="$(grep -rlE 'Flask\(__name__|= *Flask\(' --include=*.py . 2>/dev/null | head -n1)"
    if [ -n "$FLASKF" ]; then
      echo "[dev-be] flask run --debug ($FLASKF)" >&2
      exec env FLASK_APP="$FLASKF" FLASK_DEBUG=1 flask run --host 0.0.0.0 --port "$PORT"
    fi
    for c in main.py app.py server.py; do [ -f "$c" ] && exec python "$c"; done
  fi

  # Go --------------------------------------------------------------------------
  if [ -f "$ROOT/go.mod" ]; then
    echo "[dev-be] go build" >&2
    (go build -o /tmp/devapp ./... 2>&1 || go build -o /tmp/devapp . 2>&1) | tail -n 80 >&2 || true
    [ -x /tmp/devapp ] && exec /tmp/devapp
  fi

  # Java (Spring Boot) ----------------------------------------------------------
  if [ -f "$ROOT/pom.xml" ]; then
    echo "[dev-be] mvn spring-boot:run" >&2
    exec mvn -q spring-boot:run -Dspring-boot.run.jvmArguments="-Dserver.port=$PORT"
  fi

  # Nothing runnable yet — a placeholder /health server keeps the container up so the
  # bootstrap turn can build the real backend, then restart_dev_server picks it up.
  echo "[dev-be] no known stack; placeholder health server on $PORT" >&2
  exec python3 -c "import http.server,os,socketserver
port=int(os.environ.get('PORT','8080'))
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
        self.wfile.write(b'{\"status\":\"starting\",\"placeholder\":true}')
    def log_message(self,*a): pass
socketserver.TCPServer(('0.0.0.0',port),H).serve_forever()"
}

start_backend
""".replace("%WORK%", _WORK).replace("%SEED%", _SEED).replace(
    "# __ANTHROPIC_PROXY_BOOTSTRAP__", ANTHROPIC_RETRY_PROXY_BOOTSTRAP
)


class DevTurnResult:
    """Outcome of one exec'd claude turn (mirrors dev_service.DevTurnResult)."""

    def __init__(self, success: bool, error: Optional[str] = None,
                 usage: Optional[dict] = None, cancelled: bool = False):
        self.success = success
        self.error = error
        self.usage = usage or {}
        self.cancelled = cancelled


class DevBackendService:
    """Owns the long-running BACKEND dev container per project (start/exec/stop/heal).

    Method names mirror ``DevService`` so callers (the turn workflow, the maintenance
    daemon) can pick the service by session ``lane`` and drive it uniformly."""

    def __init__(self) -> None:
        self.docker = DOCKER_BIN
        self.image = DEV_BE_IMAGE

    # -- availability ---------------------------------------------------------
    def is_available(self) -> bool:
        if os.getenv("CODE_DEV_MODE", "1") in ("0", "false", "False", ""):
            return False
        return docker_available() and anthropic_configured()

    # -- db namespace ---------------------------------------------------------
    def provision_db(self, project_id: str):
        """Provision (idempotent) the ISOLATED dev database namespace. Fail-soft:
        returns the ProvisionResult, or None when middleware isn't applicable."""
        try:
            return middleware_service.provision_namespace(_dev_db_key(project_id))
        except Exception:  # noqa: BLE001
            logger.warning("dev backend db provision failed for %s", project_id, exc_info=True)
            return None

    # -- container lifecycle --------------------------------------------------
    def start_container(self, project_id: str, source: dict[str, bytes]) -> tuple[bool, Optional[str], dict]:
        """Seed ``source`` and start the long-running backend dev container.

        Injects the SAME runtime env deploy uses (``PORT`` / ``DATABASE_URL`` /
        ``REDIS_*``), with an async-driver-adapted URL when the backend is SQLAlchemy
        async, so what runs in dev matches what deploys. Returns ``(ok, error, info)``.
        """
        container = _dev_be_container_name(project_id)
        self._remove(container)
        _ensure_network()

        seed_dir = Path(tempfile.mkdtemp(prefix=f"dev-be-seed-{project_id[:8]}-"))
        try:
            self._write_seed(seed_dir, source)
        except Exception as exc:  # noqa: BLE001
            return False, f"failed to stage dev backend seed: {exc}", {}

        host_seed = host_workdir(seed_dir)
        cred_flags, cred_env = anthropic_agent_credentials()
        user = container_user()

        # Isolated dev database + the async-driver adaptation deploy uses.
        prov = self.provision_db(project_id)
        db_url = prov.database_url if prov and prov.applicable else None
        if db_url:
            try:
                from backend.services.code.deploy_service import _detect_async_pg_driver

                async_driver = _detect_async_pg_driver(source)
                db_url = middleware_service.container_database_url(db_url, async_driver)
            except Exception:  # noqa: BLE001
                pass

        cmd = [
            self.docker, "run", "-d", "--name", container,
            "--network", APP_NETWORK,
            "--restart", "unless-stopped",
            "--memory", DEV_BE_MEM,
            "--user", user,
            *cred_flags,
            "-e", "OPENAI_API_KEY",
            "-e", f"PORT={DEV_BE_PORT}",
            "-e", "NODE_ENV=development",
        ]
        if user != "node":
            cmd += ["-e", "HOME=/tmp"]
        if db_url:
            cmd += ["-e", f"DATABASE_URL={db_url}"]
        if prov and prov.redis_url:
            cmd += ["-e", f"REDIS_URL={prov.redis_url}"]
        if prov and prov.redis_prefix:
            cmd += ["-e", f"REDIS_PREFIX={prov.redis_prefix}"]
        for key in ("OPENAI_BASE_URL",):
            value = os.getenv(key)
            if value:
                cmd += ["-e", f"{key}={value}"]
        cmd += ["-v", f"{host_seed}:{_SEED}:ro", self.image, "bash", "-c", _DEV_BE_ENTRYPOINT]

        env = dict(os.environ, **cred_env)
        openai_key = os.getenv("OPENAI_API_KEY")
        if openai_key:
            env["OPENAI_API_KEY"] = openai_key
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
        except subprocess.TimeoutExpired:
            return False, "docker run timed out starting backend dev container", {}
        if proc.returncode != 0:
            return False, f"docker run failed: {proc.stderr[:800]}", {}

        return True, None, {
            "container_name": container,
            "internal_port": DEV_BE_PORT,
            "workdir": str(seed_dir),
            "db_name": prov.db_name if prov else None,
        }

    def _write_seed(self, seed_dir: Path, source: dict[str, bytes]) -> None:
        try:
            os.chmod(seed_dir, 0o777)
        except OSError:
            pass
        root = seed_dir.resolve()
        for rel, data in (source or {}).items():
            rel = (rel or "").lstrip("/")
            if not rel:
                continue
            dest = (seed_dir / rel).resolve()
            if not str(dest).startswith(str(root)):
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data if isinstance(data, bytes) else str(data).encode("utf-8"))

    def _remove(self, container: str) -> None:
        try:
            _docker(["rm", "-f", container], 30)
        except Exception:  # noqa: BLE001
            pass

    def stop_container(self, project_id: str) -> None:
        """Tear the backend dev container down (rm -f) — credentials die with it."""
        self._remove(_dev_be_container_name(project_id))

    # -- health / status ------------------------------------------------------
    def container_status(self, project_id: str) -> dict:
        container = _dev_be_container_name(project_id)
        try:
            proc = _docker(
                ["inspect", "-f", "{{.State.Running}}|{{.RestartCount}}|{{.State.Status}}", container], 15,
            )
        except Exception:  # noqa: BLE001
            return {"present": False, "running": False, "restart_count": 0}
        if proc.returncode != 0:
            return {"present": False, "running": False, "restart_count": 0}
        parts = (proc.stdout or "").strip().split("|")
        running = parts[0].strip().lower() == "true" if parts else False
        try:
            restarts = int(parts[1]) if len(parts) > 1 else 0
        except ValueError:
            restarts = 0
        return {
            "present": True, "running": running, "restart_count": restarts,
            "state": parts[2] if len(parts) > 2 else "",
        }

    def health_check(self, project_id: str) -> bool:
        """Probe the backend inside the container: /health first, then / (any <500)."""
        container = _dev_be_container_name(project_id)
        probe = (
            f"code=$(curl -s -o /dev/null -w '%{{http_code}}' --max-time 5 "
            f"http://127.0.0.1:{DEV_BE_PORT}/health 2>/dev/null || echo 000); "
            f"if [ \"$code\" = 000 ] || [ \"$code\" -ge 500 ] 2>/dev/null; then "
            f"code=$(curl -s -o /dev/null -w '%{{http_code}}' --max-time 5 "
            f"http://127.0.0.1:{DEV_BE_PORT}/ 2>/dev/null || echo 000); fi; echo $code"
        )
        try:
            proc = _docker(["exec", container, "bash", "-lc", probe], 15)
        except Exception:  # noqa: BLE001
            return False
        code = (proc.stdout or "").strip()[-3:]
        return code.isdigit() and int(code) < 500 and code != "000"

    def container_logs(self, project_id: str, tail: int = 200, timestamps: bool = False) -> str:
        """Merged stdout+stderr of the backend dev container — ALL log types (npm
        install, dev-start.sh, the app's own logging incl. access logs + stack traces,
        the retry-proxy). ``docker logs`` captures the container's whole output stream,
        so nothing is filtered. Bounded by ``tail`` (≤2000 lines) + a 200KB byte cap."""
        try:
            tail = max(1, min(int(tail or 200), 2000))
        except (TypeError, ValueError):
            tail = 200
        args = ["logs", "--tail", str(tail)]
        if timestamps:
            args.append("--timestamps")
        args.append(_dev_be_container_name(project_id))
        try:
            proc = _docker(args, 20)
            out = (proc.stdout or "") + (proc.stderr or "")
        except Exception:  # noqa: BLE001
            return ""
        if len(out) > 200_000:
            out = "…(truncated)…\n" + out[-200_000:]
        return out

    def restart_dev_server(self, project_id: str, wait: bool = True) -> bool:
        """Restart the container so the entrypoint re-runs (re-installs deps + picks up
        a newly-written ``dev-start.sh`` / config). ``docker restart`` (NOT rm) keeps
        the container fs, so agent edits survive."""
        container = _dev_be_container_name(project_id)
        try:
            proc = _docker(["restart", "-t", "10", container], 120)
        except Exception:  # noqa: BLE001
            return False
        if proc.returncode != 0:
            return False
        return self.wait_ready(project_id) if wait else True

    def wait_ready(self, project_id: str, timeout: Optional[int] = None) -> bool:
        import time

        deadline = timeout if timeout is not None else DEV_BE_START_TIMEOUT
        waited = 0
        while waited < deadline:
            if self.health_check(project_id):
                return True
            time.sleep(3)
            waited += 3
        return self.health_check(project_id)

    # -- interactive turn -----------------------------------------------------
    def exec_turn(
        self, project_id: str, prompt: str,
        on_event: Optional[Callable[[dict], None]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
        timeout: Optional[int] = None, workdir: Optional[str] = None,
    ) -> DevTurnResult:
        """Run ONE headless ``claude -p`` edit round inside the backend dev container
        (identical exec contract to ``dev_service.exec_turn``)."""
        container = _dev_be_container_name(project_id)
        gateway = os.getenv("AGENT_ANTHROPIC_BASE_URL") or os.getenv("ANTHROPIC_BASE_URL")
        exec_env_flags: list[str] = []
        if gateway:
            proxy_port = os.getenv("ANTHROPIC_PROXY_PORT", "8788")
            exec_env_flags += ["-e", f"ANTHROPIC_BASE_URL=http://127.0.0.1:{proxy_port}"]

        cd_target = workdir or "$(cat /tmp/dev_project_root 2>/dev/null || echo %WORK%)"
        claude_cmd = (f'cd "{cd_target}" && exec claude -p %FLAGS%').replace(
            "%WORK%", _WORK).replace("%FLAGS%", _CLAUDE_FLAGS)
        cmd = [self.docker, "exec", "-i", *exec_env_flags, container, "bash", "-lc", claude_cmd]
        total_timeout = timeout if timeout is not None else DEV_BE_TURN_TIMEOUT

        result_events: list[dict] = []
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
            )
        except Exception as exc:  # noqa: BLE001
            return DevTurnResult(False, error=f"docker exec failed: {exc}")

        try:
            if proc.stdin:
                proc.stdin.write(prompt)
                proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass

        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "result":
                    result_events.append(event)
                if on_event:
                    try:
                        on_event(event)
                    except Exception:  # noqa: BLE001
                        logger.exception("dev backend turn on_event raised")
                if is_cancelled and is_cancelled():
                    self.cancel_turn(project_id)
                    proc.kill()
                    return DevTurnResult(False, error="cancelled", cancelled=True)
            proc.wait(timeout=total_timeout if total_timeout > 0 else None)
        except subprocess.TimeoutExpired:
            self.cancel_turn(project_id)
            proc.kill()
            return DevTurnResult(False, error="dev backend turn timed out")
        finally:
            stderr = proc.stderr.read() if proc.stderr else ""
            if stderr:
                logger.info("dev backend exec stderr: %s", stderr[:2000])

        usage = {}
        for ev in result_events:
            u = ev.get("usage") or {}
            if u:
                usage = u
        ok = proc.returncode == 0
        return DevTurnResult(ok, error=None if ok else f"claude exited {proc.returncode}", usage=usage)

    def cancel_turn(self, project_id: str) -> None:
        container = _dev_be_container_name(project_id)
        try:
            _docker(["exec", container, "bash", "-lc", "pkill -f 'claude' || true"], 15)
        except Exception:  # noqa: BLE001
            pass

    # -- source collection ----------------------------------------------------
    def collect_source(self, project_id: str) -> dict[str, bytes]:
        """Tar the workspace source out of the container (excludes build/dep dirs)."""
        container = _dev_be_container_name(project_id)
        excludes = " ".join(f"--exclude=./{d}" for d in _COLLECT_EXCLUDE)
        cmd = [
            self.docker, "exec", container, "bash", "-lc",
            f'cd "$(cat /tmp/dev_project_root 2>/dev/null || echo {_WORK})" && tar {excludes} -cf - . 2>/dev/null',
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=120)
        except Exception as exc:  # noqa: BLE001
            logger.warning("dev backend collect_source failed: %s", exc)
            return {}
        if proc.returncode != 0 or not proc.stdout:
            return {}
        files: dict[str, bytes] = {}
        total = 0
        try:
            with tarfile.open(fileobj=BytesIO(proc.stdout), mode="r:") as tf:
                for member in tf.getmembers():
                    if not member.isfile():
                        continue
                    rel = member.name
                    if rel.startswith("./"):
                        rel = rel[2:]
                    rel = rel.lstrip("/")
                    if not rel or rel == ".seeded":
                        continue
                    if any(part in _COLLECT_EXCLUDE for part in rel.split("/")):
                        continue
                    if member.size > _MAX_FILE_BYTES:
                        continue
                    fh = tf.extractfile(member)
                    if not fh:
                        continue
                    data = fh.read()
                    total += len(data)
                    if total > _MAX_TOTAL_BYTES:
                        break
                    files[rel] = data
        except (tarfile.TarError, OSError) as exc:
            logger.warning("dev backend collect_source tar parse failed: %s", exc)
            return {}
        return files


_dev_backend_service: Optional[DevBackendService] = None


def get_dev_backend_service() -> DevBackendService:
    """Process-level singleton (stateless; safe to share)."""
    global _dev_backend_service
    if _dev_backend_service is None:
        _dev_backend_service = DevBackendService()
    return _dev_backend_service
