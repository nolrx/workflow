"""
Frontend PROJECT build service (agentic, container-sandboxed).

Unlike ``frontend_build_service`` (one text-model call returning a single inline
index.html), this drives an autonomous coding CLI (Claude Code, headless) inside
a throwaway Docker container to produce a COMPLETE multi-file React + Vite + TS
project that the agent itself builds and self-checks.

Sandbox properties (validated by PoC):
  * runs as the non-root ``node`` user (the CLI refuses bypassPermissions as root);
  * the only host-visible write is the mounted ``/out`` dir — node_modules and the
    npm cache live only in the container fs and die with ``--rm``;
  * the deliverable copied to /out is source + built ``dist`` ONLY.

This deliberately steps OUTSIDE the capability-routed AI provider abstraction:
the CLI owns its own Anthropic backend, so this is an agent-EXECUTION lane, not a
single ``generate_text`` call. Token usage is reported back via the CLI's terminal
``result`` event so the caller can meter it into the credit system.
"""
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).parent.parent.parent / "prompts" / "code"

# Per-file ceiling when collecting the deliverable, so a runaway file can't blow
# up the DB / artifact storage.
_MAX_FILE_BYTES = 512_000

# Runs INSIDE the container. The agent's stream-json goes to stdout (streamed live
# to the host); an in-container ``timeout`` guards against a hung agent. Only
# source + built dist are copied to the mounted /out.
_CONTAINER_SCRIPT = r"""
WORK=/tmp/work
mkdir -p "$WORK" && cd "$WORK"
timeout "${FE_AGENT_TIMEOUT:-840}" claude -p "$(cat /out/prompt.txt)" \
  --output-format stream-json --verbose \
  --permission-mode bypassPermissions \
  --allowedTools Read Write Edit Bash
echo "$?" > /out/claude_exit
mkdir -p /out/project
cp -r src /out/project/ 2>/dev/null
cp index.html package.json vite.config.ts tsconfig*.json /out/project/ 2>/dev/null
[ -d dist ] && cp -r dist /out/project/dist
"""


class FrontendProjectService:
    """Generate a runnable multi-file frontend project via a sandboxed agent."""

    def __init__(self):
        self.image = os.getenv("FE_AGENT_IMAGE", "fe-agent:latest")
        self.timeout = int(os.getenv("FE_AGENT_TIMEOUT", "900"))
        self.docker = os.getenv("DOCKER_BIN", "docker")

    # --- prompt assembly -----------------------------------------------------
    def _load_prompt(self, name: str) -> str:
        with open(PROMPT_DIR / name, "r", encoding="utf-8") as fh:
            return fh.read()

    @staticmethod
    def _fill(template: str, **values: str) -> str:
        out = template
        for key, value in values.items():
            out = out.replace(f"[[{key}]]", value if value is not None else "")
        return out

    def is_configured(self) -> bool:
        return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY"))

    # --- public API ----------------------------------------------------------
    def build_project(
        self,
        *,
        requirement: str,
        requirements_doc: str = "",
        development_flow: str = "",
        documents_digest: str = "",
        style_prompt: str = "",
        ui_baseline_prompt: str = "",
        context_ledger: str = "",
        on_event: Optional[Callable[[dict], None]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> dict:
        """Run the containerized agent.

        Returns ``{success, error, files, dist_files, summary, usage, cost_usd,
        workdir}``. Never raises on agent/model failure (``success=False``);
        raises only on infrastructure errors (docker missing, timeout).
        """
        if not self.is_configured():
            return self._empty("ANTHROPIC_API_KEY not configured")

        prompt = self._fill(
            self._load_prompt("frontend_project_prompt.txt"),
            CONTEXT_LEDGER=context_ledger or "",
            REQUIREMENT=requirement or "",
            REQUIREMENTS_DOC=requirements_doc or "",
            DEVELOPMENT_FLOW=development_flow or "",
            DOCUMENTS=documents_digest or "",
            STYLE_PROMPT=style_prompt or "",
            UI_BASELINE=ui_baseline_prompt or "",
        )

        workdir = Path(tempfile.mkdtemp(prefix="fe-agent-"))
        # The container runs as the non-root `node` user (uid 1000) and writes
        # the deliverable to this mounted dir. mkdtemp is 0700 (host user only);
        # on real Linux the container uid couldn't write it (Docker Desktop for
        # Mac is permissive and hides this), so open it up explicitly.
        os.chmod(workdir, 0o777)
        (workdir / "prompt.txt").write_text(prompt, encoding="utf-8")
        api_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY")

        cmd = [
            self.docker, "run", "--rm", "--user", "node",
            "-e", "ANTHROPIC_API_KEY",
            "-e", f"FE_AGENT_TIMEOUT={max(60, self.timeout - 60)}",
            "-v", f"{workdir}:/out",
            self.image, "bash", "-c", _CONTAINER_SCRIPT,
        ]
        env = dict(os.environ, ANTHROPIC_API_KEY=api_key or "")

        result_event = None
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env,
        )
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue  # non-JSON noise on stdout — ignore
                if event.get("type") == "result":
                    result_event = event
                if on_event:
                    try:
                        on_event(event)
                    except Exception:  # noqa: BLE001 - a bad callback must not kill the run
                        logger.exception("on_event callback raised")
                if is_cancelled and is_cancelled():
                    proc.kill()
                    return self._empty("cancelled")
            proc.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise RuntimeError("frontend project agent timed out")
        finally:
            stderr = proc.stderr.read() if proc.stderr else ""
            if stderr:
                logger.info("fe-agent stderr: %s", stderr[:2000])

        files = self._collect(workdir / "project", exclude_top="dist")
        dist_files = self._collect(workdir / "project" / "dist")

        summary, usage, cost, is_error = "", {}, 0.0, True
        if result_event:
            raw = result_event.get("result")
            summary = raw if isinstance(raw, str) else ""
            usage = result_event.get("usage") or {}
            cost = float(result_event.get("total_cost_usd") or 0.0)
            is_error = bool(result_event.get("is_error"))

        success = (not is_error) and bool(files) and bool(dist_files)
        return {
            "success": success,
            "error": None if success else "agent did not produce a buildable project",
            "files": files,
            "dist_files": dist_files,
            "summary": summary,
            "usage": usage,
            "cost_usd": cost,
            "workdir": str(workdir),
        }

    # --- helpers -------------------------------------------------------------
    @staticmethod
    def _empty(error: str) -> dict:
        return {
            "success": False, "error": error, "files": {}, "dist_files": {},
            "summary": "", "usage": {}, "cost_usd": 0.0, "workdir": None,
        }

    def _collect(self, root: Path, exclude_top: Optional[str] = None) -> dict:
        """Return ``{relative_path: text}`` for text files under ``root``."""
        out: dict[str, str] = {}
        if not root.exists():
            return out
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if exclude_top and rel.parts and rel.parts[0] == exclude_top:
                continue
            try:
                if path.stat().st_size > _MAX_FILE_BYTES:
                    continue
                out[str(rel)] = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                out[str(rel)] = ""  # binary/unreadable — keep the path as a marker
        return out


_service_instance: Optional[FrontendProjectService] = None


def get_frontend_project_service() -> FrontendProjectService:
    global _service_instance
    if _service_instance is None:
        _service_instance = FrontendProjectService()
    return _service_instance
