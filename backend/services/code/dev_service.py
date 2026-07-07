"""
Dev Mode container lifecycle service — the long-running interactive dev sandbox.

Unlike ``frontend_project_service`` (which runs a ``docker run --rm`` throwaway
build container and dies), Dev Mode needs a LONG-RUNNING container that runs
``npm run dev`` (Vite + HMR) and stays alive across many interactive turns. Each
user turn is a bounded ``docker exec`` of the headless ``claude -p`` coding CLI
against the SAME running container's workspace; Vite's HMR then hot-reloads the
right-pane preview within seconds.

This deliberately mirrors ``deploy_service._run_container``'s proven long-lived
pattern (``docker run -d --restart unless-stopped`` on the shared app network),
but with the dev server as the main process instead of the built backend image.

The adversarial design review's must-fix items are baked in here (see
docs/code-dev-mode.md §11.1):
  * (#2) the gateway retry-proxy is started in the container ENTRYPOINT (persists
    for the container's life) and every ``docker exec`` overrides
    ``ANTHROPIC_BASE_URL`` to the in-container proxy — otherwise exec'd claude
    calls would bypass the proxy and hit the flaky gateway directly (~50% 403).
  * (#1) cancel kills the IN-CONTAINER claude process (``pkill``), not just the
    local ``docker exec`` client — and turns are serialized per session.
  * (#3/#4) the dev container is seeded read-only, source lives in the container
    fs (node_modules never lands on the host bind mount), a merged Vite config
    fixes ``base``/``allowedHosts``/``hmr`` for the ``/preview/<pid>/`` subpath.
  * (#7) one per-project container (``dev-<pid>``); ``stop`` does ``rm -f`` so the
    credentials in its env / codex auth.json die with it.

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

from backend.services.code import asset_lane, middleware_service
from backend.services.code.docker_env import (
    ANTHROPIC_RETRY_PROXY_BOOTSTRAP,
    anthropic_agent_credentials,
    anthropic_configured,
    container_user,
    host_workdir,
)

# Where the dev container keeps asset-lane state/diagnostics (no /out mount here).
ASSET_STATE_DIR = "/tmp/dev-assets"

logger = logging.getLogger(__name__)

DOCKER_BIN = os.getenv("DOCKER_BIN", "docker")
APP_NETWORK = os.getenv("APP_NETWORK", "worksflow-net")
DEV_IMAGE = os.getenv("FE_AGENT_IMAGE", "fe-agent:latest")
DEV_PORT = int(os.getenv("DEV_MODE_VITE_PORT", "5173"))
DEV_MEM = os.getenv("DEV_MODE_CONTAINER_MEM", "1024m")
# The public port the browser's HMR websocket client should connect back through
# (the platform is reached via nginx on 443/80). Ops must set this to match the
# public scheme; default 443 (https). Paired with DEV_MODE_HMR_PROTOCOL.
DEV_HMR_CLIENT_PORT = int(os.getenv("DEV_MODE_HMR_CLIENT_PORT", "443"))
DEV_HMR_PROTOCOL = os.getenv("DEV_MODE_HMR_PROTOCOL", "wss")
# npm install can be slow on a cold registry; generous but bounded.
DEV_INSTALL_TIMEOUT = int(os.getenv("DEV_MODE_INSTALL_TIMEOUT", "600"))
DEV_START_TIMEOUT = int(os.getenv("DEV_MODE_START_TIMEOUT", "120"))
# Per-turn wall-clock cap for the exec'd claude round (0 = uncapped, bounded only
# by cooperative cancel / the agent finishing).
DEV_TURN_TIMEOUT = int(os.getenv("DEV_MODE_TURN_TIMEOUT", "0"))
# Post-edit runtime browser smoke against the LIVE dev server (P3 parallel gate).
DEV_SMOKE_TIMEOUT = int(os.getenv("DEV_MODE_SMOKE_TIMEOUT", "90"))
DEV_SMOKE_MAX_INTERACTIONS = int(os.getenv("DEV_MODE_SMOKE_MAX_INTERACTIONS", "20"))
# Idle window after which a running dev session is reaped (container removed).
DEV_IDLE_REAP_SECONDS = int(os.getenv("DEV_MODE_IDLE_REAP_SECONDS", "3600"))

# Per-file / total ceilings when collecting the workspace source for verify /
# deploy, so a runaway file can't blow up memory. Mirrors frontend_project_service.
_MAX_FILE_BYTES = int(os.getenv("DEV_MODE_MAX_FILE_BYTES", str(2_000_000)))
_MAX_TOTAL_BYTES = int(os.getenv("DEV_MODE_MAX_TOTAL_BYTES", str(48_000_000)))
_COLLECT_EXCLUDE = ("node_modules", ".git", ".cache", ".npm", "dist")
# Built-dist collection needs bigger per-file room than SOURCE (entry bundles can be
# several MB) — mirrors the FE preview bundle-cap fix (memory fe-preview-bundle-cap-drop).
_DIST_MAX_FILE_BYTES = int(os.getenv("DEV_MODE_DIST_MAX_FILE_BYTES", str(16_000_000)))
_DIST_MAX_TOTAL_BYTES = int(os.getenv("DEV_MODE_DIST_MAX_TOTAL_BYTES", str(64_000_000)))

# Headless claude flags — identical to the build container so behaviour matches.
_CLAUDE_FLAGS = (
    "--output-format stream-json --verbose "
    "--permission-mode bypassPermissions --allowedTools Read Write Edit Bash Skill"
)

# The container workspace (container fs, NOT a host bind mount — see module docs).
_WORK = "/tmp/work"
# Read-only seed mount: the initial source is bind-mounted here and copied into
# /work ONCE (so restarts keep the agent's edits, not re-seed over them).
_SEED = "/tmp/seed"


def _dev_container_name(project_id: str) -> str:
    """Per-project dev container name on the shared network (distinct from app-<pid>)."""
    slug = middleware_service._sanitized_db_name(project_id)[4:]
    return f"dev-{slug}"[:60] or f"dev-{project_id[:12]}"


def _docker(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [DOCKER_BIN, *args], capture_output=True, text=True, timeout=timeout
    )


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


# The container entrypoint. Runs as the container's main process, so:
#   * it stays alive as long as `npm run dev` (Vite) runs → the retry proxy
#     started here (backgrounded) also stays alive for the container's life;
#   * on Vite crash the process exits → `--restart unless-stopped` restarts it →
#     the seed is NOT re-copied (guarded by /work/.seeded) so agent edits survive.
# ${DEV_BASE}, ${DEV_PORT}, ${DEV_HMR_CLIENT_PORT}, ${DEV_HMR_PROTOCOL} come from
# the container env (docker run -e). The gateway retry-proxy bootstrap is spliced
# in via the marker so exec'd claude calls (which override ANTHROPIC_BASE_URL to
# the proxy) reach a live proxy.
_DEV_ENTRYPOINT = r"""
set -u
export HOME="${HOME:-/home/node}"
export PATH="$HOME/bin:$PATH"
mkdir -p "$HOME/bin" "$HOME/.claude/skills/image-assets" "$HOME/.codex" 2>/dev/null || true

# __ANTHROPIC_PROXY_BOOTSTRAP__

# __ASSET_LANE_BOOTSTRAP__

# Seed the workspace ONCE from the read-only /seed mount (keeps agent edits on restart).
mkdir -p "%WORK%"
if [ ! -f "%WORK%/.seeded" ]; then
  if [ -d "%SEED%" ] && [ -n "$(ls -A %SEED% 2>/dev/null)" ]; then
    cp -a %SEED%/. "%WORK%"/ 2>/dev/null || true
  fi
  touch "%WORK%/.seeded"
fi
cd "%WORK%"

# Locate the project root (dir with package.json), tolerate one level of nesting.
ROOT="%WORK%"
if [ ! -f "$ROOT/package.json" ]; then
  found="$(find "%WORK%" -maxdepth 3 -name package.json -not -path '*/node_modules/*' 2>/dev/null | head -n1)"
  [ -n "$found" ] && ROOT="$(dirname "$found")"
fi
cd "$ROOT"
echo "$ROOT" > /tmp/dev_project_root

# A merged Vite dev config: keep the project's own config (plugins etc.) and layer
# the reverse-proxy-correct server settings on top (base / allowedHosts / hmr).
cat > "$ROOT/vite.dev-mode.config.mjs" <<'DEVCFG'
import { loadConfigFromFile, mergeConfig } from 'vite'
export default async () => {
  let userConfig = {}
  try {
    const loaded = await loadConfigFromFile({ command: 'serve', mode: 'development' }, undefined, process.cwd())
    if (loaded && loaded.config) {
      userConfig = typeof loaded.config === 'function'
        ? await loaded.config({ command: 'serve', mode: 'development' })
        : loaded.config
    }
  } catch (e) {
    console.error('[dev-mode] loadConfigFromFile failed, using bare config:', e && e.message)
  }
  const base = process.env.DEV_BASE || '/'
  return mergeConfig(userConfig, {
    base,
    server: {
      host: '0.0.0.0',
      port: Number(process.env.DEV_PORT || 5173),
      strictPort: true,
      allowedHosts: true,
      cors: true,
      watch: { usePolling: true, interval: 300 },
      // HMR over the reverse-proxy subpath. Do NOT set `hmr.path`: with a path
      // (and no explicit hmr.port) Vite spins up a SEPARATE ws server on
      // hmr.port(=clientPort=443), so upgrades forwarded to the dev port never
      // reach it (verified: all ws handshakes time out). Omitting path keeps the
      // HMR ws on the MAIN dev-server port (gated by the `vite-hmr` subprotocol),
      // which nginx @preview_ws forwards to. The client still derives its ws URL
      // from `base` + clientPort/protocol → wss://<host>:443/preview/<pid>/.
      hmr: {
        clientPort: Number(process.env.DEV_HMR_CLIENT_PORT || 443),
        protocol: process.env.DEV_HMR_PROTOCOL || 'wss',
      },
    },
  })
}
DEVCFG

# Git baseline for parallel-lane worktrees (fail-soft; no git in image → parallel
# development safely degrades to serial). Committing --allow-empty is idempotent on
# restart. node_modules/dist are ignored so commits stay small.
if command -v git >/dev/null 2>&1; then
  if ! git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    printf 'node_modules/\ndist/\n.cache/\n.npm/\n*.log\n' > "$ROOT/.gitignore"
    git -C "$ROOT" init -q >/dev/null 2>&1 || true
    git -C "$ROOT" config user.email agent@worksflow.local >/dev/null 2>&1 || true
    git -C "$ROOT" config user.name worksflow-agent >/dev/null 2>&1 || true
  fi
  git -C "$ROOT" add -A >/dev/null 2>&1 || true
  git -C "$ROOT" commit -q -m "dev-base" --allow-empty >/dev/null 2>&1 || true
fi

# ALWAYS install deps (fast on a warm layer, ~1s when up-to-date) — a turn may have
# added/removed packages, and a restart must pick that up. This is also why the dev
# server is restarted after a turn that touches package.json / vite.config (see
# dev_service.restart_dev_server): Vite only re-reads its config on process restart,
# so a stale plugin (e.g. removed @vitejs/plugin-react still referenced) 500s otherwise.
echo "[dev-mode] npm install ..." >&2
npm install --no-audit --no-fund --prefer-offline 2>&1 | tail -n 200 >&2 || \
  echo "[dev-mode] npm install returned non-zero" >&2

# Last-resort: if the seeded project didn't bring vite (a non-Vite / scaffold-only
# prior artifact — the caller normally swaps in the minimal scaffold, this is belt-
# and-suspenders), install a pinned vite + react plugin so the dev server can start.
if [ ! -x "$ROOT/node_modules/.bin/vite" ]; then
  echo "[dev-mode] vite missing after install — installing vite@^5 + @vitejs/plugin-react…" >&2
  npm install --no-audit --no-fund "vite@^5" @vitejs/plugin-react 2>&1 | tail -n 60 >&2 || true
fi

VITE_BIN="$ROOT/node_modules/.bin/vite"
[ -x "$VITE_BIN" ] || VITE_BIN="npx --yes vite@^5"
echo "[dev-mode] starting dev server on :${DEV_PORT:-5173} base=${DEV_BASE:-/}" >&2
exec $VITE_BIN --config "$ROOT/vite.dev-mode.config.mjs" --host 0.0.0.0 --port "${DEV_PORT:-5173}"
""".replace("%WORK%", _WORK).replace("%SEED%", _SEED).replace(
    "# __ANTHROPIC_PROXY_BOOTSTRAP__", ANTHROPIC_RETRY_PROXY_BOOTSTRAP
).replace(
    # The SHARED asset lane (same module as the one-shot fe-agent build): full
    # image-assets skill + gen-assets + genimage.mjs + Codex login. Dev has no
    # /out mount, so state/diagnostics live under /tmp/dev-assets and the style
    # context is a pre-distilled file written per asset task (write_asset_context).
    "# __ASSET_LANE_BOOTSTRAP__",
    asset_lane.render_bootstrap(
        style_context_path=ASSET_STATE_DIR + "/style_context.txt",
        diagnostics_dir=ASSET_STATE_DIR,
        style_extract="raw",
    ),
)


# Node/Playwright harness for the post-edit runtime smoke, run INSIDE the dev
# container against the LIVE Vite dev server (not a built dist). Loads the app in a
# headless browser, drives a bounded interactive crawl, and writes a runtime_check
# JSON whose shape matches `_verify_support.runtime_errors`. Fail-OPEN: no browser
# module / a bare navigation timeout with NO captured errors → {"ran": false} so it
# never blocks on infrastructure — it only reports errors the browser actually saw.
_DEV_SMOKE_SCRIPT = r"""
import fs from 'node:fs';
import { createRequire } from 'node:module';
const RESULT = '/tmp/dev_smoke_result.json';
const write = (o) => { try { fs.writeFileSync(RESULT, JSON.stringify(o)); } catch {} };
const URL = process.env.DEV_SMOKE_URL || 'http://127.0.0.1:5173/';
const requireSmoke = createRequire('/opt/runtime-smoke/');
let launcher = null;
try { launcher = (await import('playwright')).chromium; } catch {}
if (!launcher) { try { launcher = requireSmoke('playwright').chromium; } catch {} }
if (!launcher) { try { launcher = requireSmoke('playwright-core').chromium; } catch {} }
if (!launcher) { try { const pp = await import('puppeteer'); launcher = pp.default || pp; } catch {} }
if (!launcher) { write({ ran: false, reason: 'no headless browser module' }); process.exit(0); }
const consoleErrors = [], pageErrors = [];
const timeoutMs = Number(process.env.DEV_SMOKE_TIMEOUT || 60) * 1000;
const MAXI = Number(process.env.DEV_SMOKE_MAX_INTERACTIONS || 20);
const uniq = (a) => [...new Set(a)].slice(0, 30);
let browser = null;
try {
  browser = await launcher.launch({ args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'] });
  const page = await browser.newPage();
  page.on('console', (m) => { try { if (m.type() === 'error') consoleErrors.push(String(m.text()).slice(0, 300)); } catch {} });
  page.on('pageerror', (e) => { try { pageErrors.push(String((e && e.message) || e).slice(0, 300)); } catch {} });
  page.on('dialog', (d) => { try { d.dismiss().catch(() => {}); } catch {} });
  page.on('popup', (p) => { try { p.close().catch(() => {}); } catch {} });
  await page.goto(URL, { waitUntil: 'load', timeout: timeoutMs });
  await new Promise((r) => setTimeout(r, 1500));
  const HOME = URL.replace(/\/$/, '');
  const interactions = { total: 0, clicked: 0, filled: 0, dead_controls: [] };
  const deadline = Date.now() + Math.min(timeoutMs, 60000);
  try {
    const fields = await page.$$('input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=checkbox]):not([type=radio]):not([type=file]), textarea');
    for (const el of fields.slice(0, 12)) {
      try {
        const t = (await el.getAttribute('type')) || 'text';
        const v = t === 'email' ? 'demo@example.com' : t === 'password' ? 'Demo1234!' : t === 'number' ? '1' : t === 'tel' ? '13800000000' : 'test';
        await el.fill(v, { timeout: 1500 }); interactions.filled++;
      } catch {}
    }
    const controls = await page.$$('button:not([disabled]), [role="button"], a[href]:not([href=""]):not([href="#"]), input[type="submit"], [onclick]');
    interactions.total = controls.length;
    for (let i = 0; i < Math.min(controls.length, MAXI); i++) {
      if (Date.now() > deadline) break;
      const el = controls[i];
      let label = ''; try { label = ((await el.innerText().catch(() => '')) || (await el.getAttribute('aria-label').catch(() => '')) || '').replace(/\s+/g, ' ').trim().slice(0, 40); } catch {}
      let beforeLen = 0; try { beforeLen = await page.evaluate(() => (document.body ? document.body.innerHTML.length : 0)); } catch {}
      const beforeUrl = page.url(); const errBefore = consoleErrors.length + pageErrors.length;
      let net = false; const onReq = () => { net = true; }; try { page.on('request', onReq); } catch {}
      try { await el.click({ timeout: 2000 }); interactions.clicked++; await new Promise((r) => setTimeout(r, 300)); } catch {}
      try { page.off('request', onReq); } catch {}
      try { if (!page.url().startsWith(HOME)) { await page.goto(URL, { waitUntil: 'load', timeout: timeoutMs }); await new Promise((r) => setTimeout(r, 250)); continue; } } catch {}
      let afterLen = beforeLen; try { afterLen = await page.evaluate(() => (document.body ? document.body.innerHTML.length : 0)); } catch {}
      const errAfter = consoleErrors.length + pageErrors.length;
      const changed = afterLen !== beforeLen || page.url() !== beforeUrl || net || errAfter !== errBefore;
      if (!changed && label) interactions.dead_controls.push(label);
    }
  } catch (e) { interactions.error = String((e && e.message) || e).slice(0, 200); }
  interactions.dead_controls = [...new Set(interactions.dead_controls)].slice(0, 20);
  const ce = uniq(consoleErrors), pe = uniq(pageErrors);
  write({ ran: true, ok: ce.length === 0 && pe.length === 0, console_errors: ce, page_errors: pe, interactions });
} catch (e) {
  // A navigation/harness failure with NO real errors captured is fail-SOFT (ran:false);
  // genuine JS errors seen before the failure still block.
  const ce = uniq(consoleErrors), pe = uniq(pageErrors);
  if (ce.length || pe.length) write({ ran: true, ok: false, console_errors: ce, page_errors: pe });
  else write({ ran: false, reason: 'smoke harness error: ' + ((e && e.message) || e).slice(0, 160) });
} finally { try { if (browser) await browser.close(); } catch {} }
"""


class DevTurnResult:
    """Outcome of one exec'd claude turn."""

    def __init__(self, success: bool, error: Optional[str] = None,
                 usage: Optional[dict] = None, cancelled: bool = False):
        self.success = success
        self.error = error
        self.usage = usage or {}
        self.cancelled = cancelled


class DevService:
    """Owns the long-running dev container per project (start / exec / stop / heal)."""

    def __init__(self) -> None:
        self.docker = DOCKER_BIN
        self.image = DEV_IMAGE

    # -- availability ---------------------------------------------------------
    def is_available(self) -> bool:
        """Dev Mode needs docker AND an Anthropic credential for the coding CLI.

        Also honours the ``CODE_DEV_MODE`` ops kill-switch (default ON): set
        ``CODE_DEV_MODE=0`` to hard-disable the feature without a code change."""
        if os.getenv("CODE_DEV_MODE", "1") in ("0", "false", "False", ""):
            return False
        return docker_available() and anthropic_configured()

    # -- container lifecycle --------------------------------------------------
    def start_container(self, project_id: str, source: dict[str, bytes]) -> tuple[bool, Optional[str], dict]:
        """Materialize ``source`` as a read-only seed and start the long-running dev container.

        Returns ``(ok, error, info)`` where ``info`` carries ``container_name`` /
        ``internal_port`` / ``workdir`` (the host seed dir) for the session row.
        ``source`` is ``{relpath: bytes}`` — the project skeleton or the last built
        frontend source. Safe to call to (re)start: removes any existing container.
        """
        container = _dev_container_name(project_id)
        self._remove(container)
        _ensure_network()

        # Stage the seed source into a host-visible dir (bind-mounted read-only).
        seed_dir = Path(tempfile.mkdtemp(prefix=f"dev-seed-{project_id[:8]}-"))
        try:
            self._write_seed(seed_dir, source)
        except Exception as exc:  # noqa: BLE001
            return False, f"failed to stage dev seed: {exc}", {}

        host_seed = host_workdir(seed_dir)
        cred_flags, cred_env = anthropic_agent_credentials()
        user = container_user()

        base_path = f"/preview/{project_id}/"
        cmd = [
            self.docker, "run", "-d", "--name", container,
            "--network", APP_NETWORK,
            "--restart", "unless-stopped",
            "--memory", DEV_MEM,
            "--user", user,
            *cred_flags,
            "-e", "OPENAI_API_KEY",
            "-e", f"DEV_BASE={base_path}",
            "-e", f"DEV_PORT={DEV_PORT}",
            "-e", f"DEV_HMR_CLIENT_PORT={DEV_HMR_CLIENT_PORT}",
            "-e", f"DEV_HMR_PROTOCOL={DEV_HMR_PROTOCOL}",
        ]
        if user != "node":
            cmd += ["-e", "HOME=/tmp"]
        # Image-lane env (base url / model / quality / size / codex+genimage
        # timeouts) — the shared asset lane's full contract; key stays by-name.
        cmd += asset_lane.docker_env_flags()
        cmd += ["-v", f"{host_seed}:{_SEED}:ro", self.image, "bash", "-c", _DEV_ENTRYPOINT]

        env = dict(os.environ, **cred_env)
        openai_key = os.getenv("OPENAI_API_KEY")
        if openai_key:
            env["OPENAI_API_KEY"] = openai_key
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
        except subprocess.TimeoutExpired:
            return False, "docker run timed out starting dev container", {}
        if proc.returncode != 0:
            return False, f"docker run failed: {proc.stderr[:800]}", {}

        return True, None, {
            "container_name": container,
            "internal_port": DEV_PORT,
            "workdir": str(seed_dir),
            "preview_path": base_path,
        }

    def _write_seed(self, seed_dir: Path, source: dict[str, bytes]) -> None:
        """Write the seed source safely (no path traversal) into ``seed_dir``."""
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
                continue  # path traversal guard
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data if isinstance(data, bytes) else str(data).encode("utf-8"))

    def _remove(self, container: str) -> None:
        try:
            _docker(["rm", "-f", container], 30)
        except Exception:  # noqa: BLE001
            pass

    def stop_container(self, project_id: str) -> None:
        """Tear the dev container down (rm -f) — credentials in its env die with it."""
        self._remove(_dev_container_name(project_id))

    # -- health / status ------------------------------------------------------
    def container_status(self, project_id: str) -> dict:
        """``docker inspect`` the dev container: running + restart count, or absent."""
        container = _dev_container_name(project_id)
        try:
            proc = _docker(
                ["inspect", "-f", "{{.State.Running}}|{{.RestartCount}}|{{.State.Status}}", container],
                15,
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
            "present": True,
            "running": running,
            "restart_count": restarts,
            "state": parts[2] if len(parts) > 2 else "",
        }

    def health_check(self, project_id: str) -> bool:
        """Probe the Vite dev server inside the container (HTTP 200-ish on base)."""
        container = _dev_container_name(project_id)
        base = f"http://127.0.0.1:{DEV_PORT}/"
        try:
            proc = _docker(
                ["exec", container, "bash", "-lc",
                 f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 5 {base} || echo 000"],
                15,
            )
        except Exception:  # noqa: BLE001
            return False
        code = (proc.stdout or "").strip()[-3:]
        return code.isdigit() and int(code) < 500 and code != "000"

    def runtime_smoke(self, project_id: str) -> dict:
        """Post-edit runtime browser smoke against the LIVE dev server (P3 parallel gate).

        Loads the merged app in a headless browser inside the container, drives a bounded
        interactive crawl, and returns a ``runtime_check`` dict (shape per
        ``_verify_support.runtime_errors``: ``ran`` / ``ok`` / ``console_errors`` /
        ``page_errors`` / ``interactions``). This upgrades the batch objective gate from
        "house rules only" to "the app actually loads and can be driven".

        Fail-OPEN: any inability to run (no browser in the image, container gone, harness
        error with no captured errors) returns ``{"ran": False}`` so it NEVER blocks on
        infrastructure — it only gates on a real runtime error the browser observed."""
        container = _dev_container_name(project_id)
        url = f"http://127.0.0.1:{DEV_PORT}/"
        # Heredoc the harness in, run it (its own stdout/err → a log), read the JSON back.
        full = (
            "cat > /tmp/dev_smoke.mjs <<'DEVSMOKE_EOF'\n" + _DEV_SMOKE_SCRIPT + "\nDEVSMOKE_EOF\n"
            f"rm -f /tmp/dev_smoke_result.json; "
            f"DEV_SMOKE_URL='{url}' DEV_SMOKE_TIMEOUT={DEV_SMOKE_TIMEOUT} "
            f"DEV_SMOKE_MAX_INTERACTIONS={DEV_SMOKE_MAX_INTERACTIONS} "
            "node /tmp/dev_smoke.mjs > /tmp/dev_smoke.log 2>&1 || true; "
            "cat /tmp/dev_smoke_result.json 2>/dev/null || echo '{}'"
        )
        try:
            proc = _docker(["exec", container, "bash", "-lc", full], DEV_SMOKE_TIMEOUT + 45)
        except Exception:  # noqa: BLE001
            return {"ran": False}
        out = (proc.stdout or "").strip()
        try:
            data = json.loads(out) if out else {}
            return data if isinstance(data, dict) else {"ran": False}
        except (ValueError, TypeError):
            return {"ran": False}

    def container_logs(self, project_id: str, tail: int = 200, timestamps: bool = False) -> str:
        """Merged stdout+stderr of the dev container — ALL log types (npm install,
        Vite dev-server output, HMR, compile errors). Bounded by ``tail`` (≤2000
        lines) + a 200KB byte cap."""
        try:
            tail = max(1, min(int(tail or 200), 2000))
        except (TypeError, ValueError):
            tail = 200
        args = ["logs", "--tail", str(tail)]
        if timestamps:
            args.append("--timestamps")
        args.append(_dev_container_name(project_id))
        try:
            proc = _docker(args, 20)
            out = (proc.stdout or "") + (proc.stderr or "")
        except Exception:  # noqa: BLE001
            return ""
        if len(out) > 200_000:
            out = "…(truncated)…\n" + out[-200_000:]
        return out

    def restart_dev_server(self, project_id: str, wait: bool = True) -> bool:
        """Restart the dev container so Vite RE-READS its config + installs new deps.

        Vite only reloads its resolved config on a process restart, and the merged
        dev config loads the project's ``vite.config.*`` indirectly (so Vite doesn't
        watch it). A turn that changes ``vite.config`` / ``package.json`` (e.g. drops
        the React plugin) therefore leaves a stale plugin serving 500s until restart.
        The entrypoint re-runs ``npm install`` on start, so new deps are picked up."""
        container = _dev_container_name(project_id)
        # Explicitly (re)install deps BEFORE the restart: the container's baked
        # entrypoint may skip install when node_modules already exists, so a
        # newly-ADDED dependency wouldn't appear on a plain restart. Idempotent /
        # fast when up-to-date. Runs in the live container (workspace preserved).
        try:
            _docker(
                ["exec", container, "bash", "-lc",
                 'cd "$(cat /tmp/dev_project_root 2>/dev/null || echo %WORK%)" '
                 "&& npm install --no-audit --no-fund 2>&1 | tail -n 5".replace("%WORK%", _WORK)],
                DEV_INSTALL_TIMEOUT,
            )
        except Exception:  # noqa: BLE001
            pass
        # `docker restart` (NOT rm) preserves the container fs → the workspace / agent
        # edits survive; the entrypoint re-runs and Vite reloads its (possibly changed)
        # config. The seed re-copy is guarded by /work/.seeded, so edits aren't lost.
        try:
            proc = _docker(["restart", "-t", "10", container], 90)
        except Exception:  # noqa: BLE001
            return False
        if proc.returncode != 0:
            return False
        return self.wait_ready(project_id) if wait else True

    def wait_ready(self, project_id: str, timeout: Optional[int] = None) -> bool:
        """Poll the dev server until it serves again (after a start/restart)."""
        import time

        deadline = (timeout if timeout is not None else DEV_START_TIMEOUT)
        waited = 0
        while waited < deadline:
            if self.health_check(project_id):
                return True
            time.sleep(2)
            waited += 2
        return self.health_check(project_id)

    # -- interactive turn -----------------------------------------------------
    def exec_turn(
        self,
        project_id: str,
        prompt: str,
        on_event: Optional[Callable[[dict], None]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
        timeout: Optional[int] = None,
        workdir: Optional[str] = None,
    ) -> DevTurnResult:
        """Run ONE headless ``claude -p`` edit-mode round inside the dev container.

        The prompt goes via stdin (avoids E2BIG on large prompts). stream-json is
        parsed line-by-line and forwarded to ``on_event``. On cancel we kill the
        IN-CONTAINER claude (pkill) — not just the local exec client — so it can't
        keep editing the workspace (review must-fix #1). Turns must be serialized
        per session by the caller.

        ``workdir`` overrides the edit directory (used for parallel-lane git
        worktrees, e.g. ``/tmp/work-lanes/lane-0``); defaults to the detected project
        root. This method is DB-free (pure subprocess) so it is safe to call from a
        fan-out worker thread — the caller emits events on the main thread from a
        thread-safe queue (mirrors _verify_support.run_reviewers)."""
        container = _dev_container_name(project_id)
        # Override ANTHROPIC_BASE_URL to the in-container retry proxy (started by the
        # entrypoint) so exec'd claude calls are reliable (review must-fix #2). Only
        # when a gateway is configured; the official API path is left untouched.
        gateway = os.getenv("AGENT_ANTHROPIC_BASE_URL") or os.getenv("ANTHROPIC_BASE_URL")
        exec_env_flags: list[str] = []
        if gateway:
            proxy_port = os.getenv("ANTHROPIC_PROXY_PORT", "8788")
            exec_env_flags += ["-e", f"ANTHROPIC_BASE_URL=http://127.0.0.1:{proxy_port}"]

        cd_target = workdir or "$(cat /tmp/dev_project_root 2>/dev/null || echo %WORK%)"
        # PATH/CODEX_HOME exports: the entrypoint's exports don't apply to a
        # `docker exec` shell, and gen-assets/codex (the image-assets skill) must
        # be resolvable from the exec'd claude too.
        claude_cmd = (
            'export PATH="$HOME/bin:$PATH" && export CODEX_HOME="${CODEX_HOME:-$HOME/.codex}" && '
            f'cd "{cd_target}" && exec claude -p %FLAGS%'
        ).replace("%WORK%", _WORK).replace("%FLAGS%", _CLAUDE_FLAGS)

        cmd = [
            self.docker, "exec", "-i",
            *exec_env_flags,
            container, "bash", "-lc", claude_cmd,
        ]
        total_timeout = timeout if timeout is not None else DEV_TURN_TIMEOUT

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
                        logger.exception("dev turn on_event raised")
                if is_cancelled and is_cancelled():
                    self.cancel_turn(project_id)
                    proc.kill()
                    return DevTurnResult(False, error="cancelled", cancelled=True)
            proc.wait(timeout=total_timeout if total_timeout > 0 else None)
        except subprocess.TimeoutExpired:
            self.cancel_turn(project_id)
            proc.kill()
            return DevTurnResult(False, error="dev turn timed out")
        finally:
            stderr = proc.stderr.read() if proc.stderr else ""
            if stderr:
                logger.info("dev exec stderr: %s", stderr[:2000])

        usage = {}
        for ev in result_events:
            u = ev.get("usage") or {}
            if u:
                usage = u
        ok = proc.returncode == 0
        return DevTurnResult(ok, error=None if ok else f"claude exited {proc.returncode}", usage=usage)

    def cancel_turn(self, project_id: str) -> None:
        """Kill the in-container claude process (not just the local exec client)."""
        container = _dev_container_name(project_id)
        try:
            _docker(["exec", container, "bash", "-lc", "pkill -f 'claude' || true"], 15)
        except Exception:  # noqa: BLE001
            pass

    # -- git worktrees (parallel-lane isolation) ------------------------------
    # Parallel subagents each edit an isolated git worktree so they never write the
    # same files concurrently (the "合并地狱" the design review flagged). After the
    # parallel edit phase the main thread merges each lane branch back into the
    # project tree (/work), with a serial-reapply fallback on conflict. All fail-soft:
    # when git is unavailable the parallel workflow degrades to serial on /work.
    def _git(self, project_id: str, script: str, timeout: int = 120):
        container = _dev_container_name(project_id)
        full = (
            'ROOT="$(cat /tmp/dev_project_root 2>/dev/null || echo %WORK%)"; '
            'cd "$ROOT" || exit 3; ' + script
        ).replace("%WORK%", _WORK)
        try:
            return _docker(["exec", container, "bash", "-lc", full], timeout)
        except Exception:  # noqa: BLE001
            return None

    def git_ready(self, project_id: str) -> bool:
        """Ensure a git repo + snapshot the current /work state as HEAD.

        Returns False when git is absent (→ caller runs lanes serially)."""
        proc = self._git(
            project_id,
            "command -v git >/dev/null 2>&1 || exit 20; "
            "git rev-parse --git-dir >/dev/null 2>&1 || { git init -q "
            "&& git config user.email agent@worksflow.local "
            "&& git config user.name worksflow-agent; }; "
            "git add -A >/dev/null 2>&1 || true; "
            "git commit -q -m pre-parallel --allow-empty >/dev/null 2>&1 || true; echo READY",
        )
        return bool(proc and proc.returncode == 0 and "READY" in (proc.stdout or ""))

    def create_worktree(self, project_id: str, lane_id: int) -> Optional[str]:
        """Create an isolated worktree ``/tmp/work-lanes/lane-<id>`` on its own branch."""
        wt = f"/tmp/work-lanes/lane-{int(lane_id)}"
        br = f"dev-lane-{int(lane_id)}"
        proc = self._git(
            project_id,
            f'rm -rf "{wt}" >/dev/null 2>&1 || true; '
            f'git worktree add -f -B "{br}" "{wt}" HEAD >/dev/null 2>&1 && echo OK',
        )
        return wt if (proc and proc.returncode == 0 and "OK" in (proc.stdout or "")) else None

    def commit_worktree(self, project_id: str, lane_id: int) -> None:
        """Commit a lane's edits onto its branch so it can be merged back."""
        wt = f"/tmp/work-lanes/lane-{int(lane_id)}"
        self._git(
            project_id,
            f'git -C "{wt}" add -A >/dev/null 2>&1 || true; '
            f'git -C "{wt}" commit -q -m "lane-{int(lane_id)}" --allow-empty >/dev/null 2>&1 || true',
        )

    def merge_lane(self, project_id: str, lane_id: int) -> tuple[bool, list[str]]:
        """Merge a lane branch into the project tree. Returns (ok, conflict_files).

        On conflict the merge is aborted (leaving /work clean) so the caller can
        re-apply that lane serially onto the already-merged base."""
        br = f"dev-lane-{int(lane_id)}"
        proc = self._git(
            project_id,
            f'if git merge --no-edit "{br}" >/dev/null 2>&1; then echo MERGED; '
            f'else echo CONFLICT; git diff --name-only --diff-filter=U; '
            f'git merge --abort >/dev/null 2>&1 || true; fi',
        )
        out = (proc.stdout or "") if proc else ""
        if "MERGED" in out:
            return True, []
        conflicts = [
            ln.strip() for ln in out.splitlines() if ln.strip() and ln.strip() != "CONFLICT"
        ]
        return False, conflicts

    def cleanup_worktrees(self, project_id: str, lane_ids: list[int]) -> None:
        """Remove all lane worktrees + branches (best-effort)."""
        parts = []
        for i in lane_ids:
            i = int(i)
            parts.append(
                f'git worktree remove -f "/tmp/work-lanes/lane-{i}" >/dev/null 2>&1 || true; '
                f'git branch -D "dev-lane-{i}" >/dev/null 2>&1 || true'
            )
        parts.append("rm -rf /tmp/work-lanes >/dev/null 2>&1 || true; git worktree prune >/dev/null 2>&1 || true")
        self._git(project_id, " ".join(parts))

    # -- asset lane (P2: context / diagnostics / output verification) ----------
    def write_asset_context(self, project_id: str, text: str) -> bool:
        """Write the style/task context gen-assets prepends to every image prompt
        (``/tmp/dev-assets/style_context.txt``). Called before an asset task's edit
        round so all generated imagery shares the project's visual baseline."""
        container = _dev_container_name(project_id)
        payload = (text or "")[:6000]
        try:
            proc = subprocess.run(
                [
                    self.docker, "exec", "-i", container, "bash", "-lc",
                    f"mkdir -p {ASSET_STATE_DIR} && cat > {ASSET_STATE_DIR}/style_context.txt",
                ],
                input=payload.encode("utf-8"), capture_output=True, timeout=30,
            )
            return proc.returncode == 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("write_asset_context failed: %s", exc)
            return False

    def asset_diagnostics(self, project_id: str) -> dict:
        """Read the asset lane's diagnostics from the running dev container —
        the evidence that explains a "no images" turn (missing codex / key /
        gen-assets vs. a genuinely failed generation)."""
        container = _dev_container_name(project_id)
        script = (
            f'echo "codex=$(cat {ASSET_STATE_DIR}/codex_path 2>/dev/null)"; '
            f'echo "gen=$(cat {ASSET_STATE_DIR}/gen_assets_path 2>/dev/null)"; '
            'echo "key=${OPENAI_API_KEY:+1}"; '
            'echo "__LOG__"; '
            f"tail -c 2000 {ASSET_STATE_DIR}/asset_gen.log 2>/dev/null; "
            'echo "__LOGIN__"; '
            f"tail -c 500 {ASSET_STATE_DIR}/codex_login.log 2>/dev/null"
        )
        out = ""
        try:
            proc = subprocess.run(
                [self.docker, "exec", container, "bash", "-lc", script],
                capture_output=True, text=True, timeout=30,
            )
            if proc.returncode == 0:
                out = proc.stdout or ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("asset_diagnostics failed: %s", exc)
        head, _, rest = out.partition("__LOG__")
        log, _, login = rest.partition("__LOGIN__")
        fields = dict(
            line.split("=", 1) for line in head.strip().splitlines() if "=" in line
        )
        log = log.strip()
        return {
            "available": bool(out),
            "codex_available": bool(fields.get("codex", "").strip()),
            "gen_assets_available": bool(fields.get("gen", "").strip()),
            "openai_key": fields.get("key", "").strip() == "1",
            "calls": log.count("invoked:"),
            "log": log[-2000:],
            "codex_login": login.strip()[-500:],
        }

    def validate_resource_outputs(self, project_id: str, resource_spec: dict) -> dict:
        """Strong post-turn verification of an asset task's ``resource_spec.outputs``:
        path safety in Python, then existence + non-zero size probed INSIDE the
        container — never trusting the model's own claim.

        ``blocking=True`` marks a defect more retries cannot fix (illegal paths, or
        required outputs missing while the lane itself is unavailable: no codex /
        no gen-assets / no OPENAI key) — the caller blocks the task instead of
        burning its retry budget."""
        import shlex

        outputs, norm_warnings = asset_lane.normalize_outputs(resource_spec)
        raw_count = len((resource_spec or {}).get("outputs") or [])
        if raw_count and not outputs:
            return {
                "ok": False, "blocking": True,
                "reason": "所有 output 路径非法(必须是 src/assets/ 下的相对位图路径):"
                          + "；".join(norm_warnings[:5]),
                "outputs": [], "diagnostics": {},
            }
        if not outputs:
            return {"ok": True, "blocking": False, "reason": "", "outputs": [], "diagnostics": {}}
        ok_paths, path_errors = asset_lane.validate_output_paths(outputs)
        if not ok_paths:
            return {
                "ok": False, "blocking": True,
                "reason": "output 路径校验失败:" + "；".join(path_errors[:5]),
                "outputs": [], "diagnostics": {},
            }

        container = _dev_container_name(project_id)
        probes = "; ".join(
            f'if [ -s {shlex.quote(o["path"])} ]; then '
            f'printf "%s|%s\\n" {shlex.quote(o["path"])} "$(stat -c%s {shlex.quote(o["path"])} 2>/dev/null || echo 1)"; '
            f'else printf "%s|0\\n" {shlex.quote(o["path"])}; fi'
            for o in outputs
        )
        script = f'cd "$(cat /tmp/dev_project_root 2>/dev/null || echo {_WORK})" && {probes}'
        sizes: dict[str, int] = {}
        probe_ok = False
        try:
            proc = subprocess.run(
                [self.docker, "exec", container, "bash", "-lc", script],
                capture_output=True, text=True, timeout=60,
            )
            probe_ok = proc.returncode == 0
            for line in (proc.stdout or "").splitlines():
                path, _, size = line.rpartition("|")
                if path:
                    try:
                        sizes[path] = int(size)
                    except ValueError:
                        sizes[path] = 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("validate_resource_outputs probe failed: %s", exc)

        results = [
            {
                "path": o["path"],
                "exists": sizes.get(o["path"], 0) > 0,
                "bytes": sizes.get(o["path"], 0),
                "required": o["required"],
            }
            for o in outputs
        ]
        missing_required = [r["path"] for r in results if r["required"] and not r["exists"]]
        ok = probe_ok and not missing_required
        blocking = False
        reason = ""
        diagnostics: dict = {}
        if missing_required or not probe_ok:
            diagnostics = self.asset_diagnostics(project_id)
            env_dead = diagnostics.get("available") and (
                not diagnostics.get("gen_assets_available")
                or not diagnostics.get("codex_available")
                or not diagnostics.get("openai_key")
            )
            if env_dead:
                blocking = True
                lacks = []
                if not diagnostics.get("gen_assets_available"):
                    lacks.append("gen-assets 不在容器 PATH")
                if not diagnostics.get("codex_available"):
                    lacks.append("容器缺 Codex CLI(镜像未重建)")
                if not diagnostics.get("openai_key"):
                    lacks.append("未配置 OPENAI_API_KEY")
                reason = "资源生成环境不可用(重试无法解决):" + "；".join(lacks)
            elif missing_required:
                reason = "必需资源缺失:" + ", ".join(missing_required[:6])
            else:
                reason = "容器内产物探测失败"
        return {
            "ok": ok, "blocking": blocking, "reason": reason,
            "outputs": results, "diagnostics": diagnostics,
        }

    # -- source collection (for verify / deploy snapshot) ---------------------
    def collect_source(self, project_id: str) -> dict[str, bytes]:
        """Tar the workspace source out of the container (excludes node_modules/dist)."""
        container = _dev_container_name(project_id)
        excludes = " ".join(f"--exclude=./{d}" for d in _COLLECT_EXCLUDE)
        cmd = [
            self.docker, "exec", container, "bash", "-lc",
            f'cd "$(cat /tmp/dev_project_root 2>/dev/null || echo {_WORK})" && tar {excludes} -cf - . 2>/dev/null',
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=120)
        except Exception as exc:  # noqa: BLE001
            logger.warning("dev collect_source failed: %s", exc)
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
                    # Strip the tar's leading "./" prefix WITHOUT eating a dotfile's
                    # own leading dot (lstrip("./") would turn ".gitignore" into
                    # "gitignore"); then drop any leading slash.
                    rel = member.name
                    if rel.startswith("./"):
                        rel = rel[2:]
                    rel = rel.lstrip("/")
                    if not rel or rel == ".seeded":  # internal seed sentinel — not source
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
            logger.warning("dev collect_source tar parse failed: %s", exc)
            return {}
        return files

    # -- warm dist build (for instant deploy) ---------------------------------
    def build_dist_in_container(self, project_id: str, base: str = "/") -> dict[str, bytes]:
        """Build a production ``dist`` INSIDE the live dev container and tar it out.

        This is the fast path: the container's ``node_modules`` is already warm from
        ``npm run dev``, so this is just a ``vite build`` (no ``npm install``) — seconds,
        not the minutes a cold throwaway build container spends re-installing. Called
        right BEFORE the container is torn down so the result can be cached and a later
        deploy is near-instant. ``base`` pins asset URLs to the ``/preview/<pid>/`` mount
        the static dist is served from. Returns ``{}`` on any failure (caller falls back
        to a cold build). Bounded by dist-specific size caps (bundles are large)."""
        container = _dev_container_name(project_id)
        safe_base = (base or "/").replace('"', "")
        # Build with the project's own vite; ``--base`` overrides for the served subpath.
        # Fall back to ``npm run build`` for a custom multi-step build. Then tar dist/.
        script = (
            'ROOT="$(cat /tmp/dev_project_root 2>/dev/null || echo %WORK%)"; cd "$ROOT" || exit 3; '
            'rm -rf dist 2>/dev/null || true; '
            'if [ -x node_modules/.bin/vite ]; then '
            '  node_modules/.bin/vite build --base="%BASE%" >/tmp/dist_build.log 2>&1 || true; '
            'else npx --yes vite@^5 build --base="%BASE%" >/tmp/dist_build.log 2>&1 || true; fi; '
            '[ -d dist ] || npm run build >>/tmp/dist_build.log 2>&1 || true; '
            '[ -d dist ] && tar -cf - dist 2>/dev/null'
        ).replace("%WORK%", _WORK).replace("%BASE%", safe_base)
        cmd = [self.docker, "exec", container, "bash", "-lc", script]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=DEV_INSTALL_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            logger.warning("dev build_dist_in_container failed: %s", exc)
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
                    # Strip the leading ``dist/`` so the cache holds the SERVED tree.
                    if rel.startswith("dist/"):
                        rel = rel[5:]
                    elif rel == "dist":
                        continue
                    if not rel or member.size > _DIST_MAX_FILE_BYTES:
                        continue
                    fh = tf.extractfile(member)
                    if not fh:
                        continue
                    data = fh.read()
                    total += len(data)
                    if total > _DIST_MAX_TOTAL_BYTES:
                        logger.warning("dev dist collect hit total cap at %s bytes", total)
                        break
                    files[rel] = data
        except (tarfile.TarError, OSError) as exc:
            logger.warning("dev build_dist_in_container tar parse failed: %s", exc)
            return {}
        return files


_dev_service: Optional[DevService] = None


def get_dev_service() -> DevService:
    """Process-level singleton (stateless; safe to share)."""
    global _dev_service
    if _dev_service is None:
        _dev_service = DevService()
    return _dev_service
