"""
Lean frontend build — compile a known-good source tree into a static ``dist``.

Unlike ``frontend_project_service`` (which runs the heavy Claude Code AGENT to
GENERATE + self-heal a project), this module does a PURE build of source that is
already complete: ``npm install`` → ``vite build``. No agent, no Anthropic
credentials, no image generation. It exists so a deploy can graduate the code a
user tuned in Dev Mode (persisted as a ``code_frontend_project_zip`` snapshot,
source-only — the dev container excludes ``dist``) into a servable static bundle.

Runs a throwaway ``docker run --rm`` on the ``fe-agent`` image (which already has
node + vite) with the source bind-mounted, mirroring the proven DooD pattern in
``frontend_project_service`` / ``dev_service``. Fully fail-soft: ANY problem
(docker absent, install/build error, unreadable output) returns ``{}`` and the
caller keeps serving the previous dist — a deploy must never break on this.

Comments in English to match the Code/core convention.
"""
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from backend.services.code.docker_env import container_user, host_workdir

logger = logging.getLogger(__name__)

DOCKER_BIN = os.getenv("DOCKER_BIN", "docker")
DIST_IMAGE = os.getenv("FE_AGENT_IMAGE", "fe-agent:latest")
# Generous but bounded: a cold npm install + a vite build of a real project.
DIST_BUILD_TIMEOUT = int(os.getenv("CODE_DEV_DIST_BUILD_TIMEOUT", "600"))

# Per-file / total ceilings when reading the built dist back (a runaway asset
# mustn't blow up memory). Bundles can be large; mirror dev_service's bundle cap.
_MAX_FILE_BYTES = int(os.getenv("CODE_DEV_DIST_MAX_FILE_BYTES", str(16_000_000)))
_MAX_TOTAL_BYTES = int(os.getenv("CODE_DEV_DIST_MAX_TOTAL_BYTES", str(64_000_000)))

# Source staged read-only under /out/src; the build copies it to a writable /tmp
# project root, installs, builds with the reverse-proxy-correct ``--base``, and
# copies the resulting dist back to /out/dist for the host to collect.
_BUILD_SCRIPT = r"""
set -u
SRC=/out/src
PROJ=/tmp/proj
mkdir -p "$PROJ"
cp -a "$SRC"/. "$PROJ"/ 2>/dev/null || true

# Locate the project root (dir with package.json), tolerate one level of nesting.
ROOT="$PROJ"
if [ ! -f "$ROOT/package.json" ]; then
  found="$(find "$PROJ" -maxdepth 3 -name package.json -not -path '*/node_modules/*' 2>/dev/null | head -n1)"
  [ -n "$found" ] && ROOT="$(dirname "$found")"
fi
cd "$ROOT" || { echo "no package.json" >&2; exit 3; }
BASE="${DIST_BASE:-/}"

echo "[dist-build] npm install ..." >&2
npm install --no-audit --no-fund --prefer-offline > /out/install.log 2>&1 || \
  echo "[dist-build] npm install non-zero" >&2

# Build straight with vite (skips a project's tsc gate — the source already ran in
# the dev server, so type-only errors must not block the deploy dist). ``--base``
# pins asset URLs to the /preview/<pid>/ mount the static dist is served from.
VITE_BIN="$ROOT/node_modules/.bin/vite"
if [ -x "$VITE_BIN" ]; then
  ( "$VITE_BIN" build --base="$BASE" ) > /out/build.log 2>&1 || true
else
  ( npx --yes vite@^5 build --base="$BASE" ) > /out/build.log 2>&1 || true
fi

# Fallback: honour the project's own build script if the direct vite build didn't
# emit a dist (e.g. a custom multi-step build).
if [ ! -d "$ROOT/dist" ]; then
  ( npm run build ) >> /out/build.log 2>&1 || true
fi

if [ -d "$ROOT/dist" ]; then
  cp -a "$ROOT/dist" /out/dist 2>/dev/null || true
  echo ok > /out/dist_ok
fi
"""


def _docker_available() -> bool:
    try:
        return subprocess.run(
            [DOCKER_BIN, "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=15,
        ).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _write_tree(root: Path, source: dict[str, bytes]) -> None:
    """Write ``{relpath: bytes}`` under ``root`` (no path traversal)."""
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o777)
    except OSError:
        pass
    base = root.resolve()
    for rel, data in (source or {}).items():
        rel = (rel or "").lstrip("/")
        if not rel:
            continue
        dest = (root / rel).resolve()
        if not str(dest).startswith(str(base)):
            continue  # path traversal guard
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data if isinstance(data, bytes) else str(data).encode("utf-8"))


def _collect(dist_dir: Path) -> dict[str, bytes]:
    """Read the built dist back into ``{relpath: bytes}`` (bounded)."""
    if not dist_dir.is_dir():
        return {}
    files: dict[str, bytes] = {}
    total = 0
    for path in sorted(dist_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > _MAX_FILE_BYTES:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        total += len(data)
        if total > _MAX_TOTAL_BYTES:
            logger.warning("dist collect hit total cap at %s bytes", total)
            break
        files[str(path.relative_to(dist_dir))] = data
    return files


def build_dist(source: dict[str, bytes], base: str = "/", timeout: Optional[int] = None) -> dict[str, bytes]:
    """Build ``source`` ({relpath: bytes}) into a static dist ({relpath: bytes}).

    ``base`` is the public sub-path the dist is served from (e.g. ``/preview/<pid>/``)
    so asset URLs resolve. Returns ``{}`` on ANY failure — the caller must fall back
    to the previous dist. Never raises.
    """
    if not source or not _docker_available():
        return {}
    workdir = Path(tempfile.mkdtemp(prefix="fe-dist-"))
    try:
        try:
            os.chmod(workdir, 0o777)
        except OSError:
            pass
        _write_tree(workdir / "src", source)

        user = container_user()
        cmd = [DOCKER_BIN, "run", "--rm", "--user", user, "-e", f"DIST_BASE={base or '/'}"]
        if user != "node":
            cmd += ["-e", "HOME=/tmp"]
        cmd += ["-v", f"{host_workdir(workdir)}:/out", DIST_IMAGE, "bash", "-c", _BUILD_SCRIPT]

        total_timeout = timeout if timeout is not None else DIST_BUILD_TIMEOUT
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=total_timeout)
        except subprocess.TimeoutExpired:
            logger.warning("dev frontend dist build timed out")
            return {}
        if proc.returncode != 0:
            logger.warning("dev frontend dist build container failed: %s", (proc.stderr or "")[:800])
            # The dist may still exist (container script is fail-soft) — try to collect.
        dist = _collect(workdir / "dist")
        if not dist:
            logger.warning("dev frontend dist build produced no dist")
        return dist
    except Exception:  # noqa: BLE001 — build helper must never sink a deploy
        logger.warning("dev frontend dist build raised", exc_info=True)
        return {}
    finally:
        try:
            import shutil

            shutil.rmtree(workdir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass
