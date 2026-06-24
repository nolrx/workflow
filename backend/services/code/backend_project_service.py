"""
Backend PROJECT build service (agentic, container-sandboxed) — the backend
counterpart of ``frontend_project_service``.

An autonomous coding CLI (Claude Code, headless) runs inside a throwaway Docker
container and produces a COMPLETE multi-file backend project that implements the
shared OpenAPI contract. Unlike the frontend service it does NOT build/run the
app (the stack is polyglot and the real build happens at deploy time from the
project's OWN Dockerfile); instead it does a light, fail-soft syntax check,
GUARANTEES a Dockerfile exists (synthesizing a stack-appropriate one if the agent
forgot), and collects the source.

Why the project ships its own Dockerfile: the tech stack is whatever the
development flow chose (Node / Python / Go / Java…), so the deploy step just
``docker build``s the generated Dockerfile. That keeps polyglot complexity inside
the generated project instead of needing one mega runtime image.

The generated backend MUST (enforced by the prompt): expose ``/health``, read
``PORT`` / ``DATABASE_URL`` / ``REDIS_URL`` from the environment, and implement
the contract's endpoints. Comments in English (Code/core convention).
"""
import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

from backend.services.prompts import prompt_store

logger = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).parent.parent.parent / "prompts" / "code"

_MAX_TEXT_BYTES = 512_000
_MAX_ASSET_BYTES = 4_000_000
_MAX_TOTAL_ASSET_BYTES = 16_000_000
_BINARY_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".bmp", ".pdf"}

# Runs INSIDE the be-agent container. Generates the backend, detects the stack,
# does a FAIL-SOFT syntax check (no dependency install — the deploy-time docker
# build does the real thing), guarantees a Dockerfile, and tars the source out.
# Phase sentinels ({"type":"be_phase",...}) narrate progress on the host timeline.
_CONTAINER_SCRIPT = r"""
WORK=/tmp/work
mkdir -p "$WORK" && cd "$WORK"

export HOME=/home/node
export CODEX_HOME="$HOME/.codex"
export PATH="$HOME/bin:$PATH"
mkdir -p "$HOME/bin" "$CODEX_HOME"

CLAUDE_FLAGS="--output-format stream-json --verbose --permission-mode bypassPermissions --allowedTools Read Write Edit Bash"

emit() { printf '%s\n' "{\"type\":\"be_phase\",\"phase\":\"$1\"}"; }

run_capped() {  # usage: run_capped <seconds> cmd [args...]
  local secs="${1:-0}"; shift
  case "$secs" in (''|*[!0-9]*) secs=0 ;; esac
  if [ "$secs" -eq 0 ]; then "$@"; else timeout "$secs" "$@"; fi
}

# --- 1. Generate ----------------------------------------------------------
# Prompt over stdin (the assembled contract + flow + docs routinely exceeds the
# 128KB single-argv limit, which would blow exec with E2BIG).
run_capped "${BE_AGENT_TIMEOUT:-900}" claude -p $CLAUDE_FLAGS < /out/prompt.txt
echo "$?" > /out/claude_exit
mkdir -p /out/project

# Locate the project root: the agent is told to init directly in $WORK, but
# autonomous agents sometimes nest one dir deep. Prefer a dir with a recognizable
# manifest (or a Dockerfile).
PROJECT_ROOT="$WORK"
if [ ! -e "$PROJECT_ROOT/package.json" ] && [ ! -e "$PROJECT_ROOT/requirements.txt" ] \
   && [ ! -e "$PROJECT_ROOT/pyproject.toml" ] && [ ! -e "$PROJECT_ROOT/go.mod" ] \
   && [ ! -e "$PROJECT_ROOT/pom.xml" ] && [ ! -e "$PROJECT_ROOT/Dockerfile" ]; then
  MANIFEST="$(find "$WORK" -maxdepth 3 \
    \( -name package.json -o -name requirements.txt -o -name pyproject.toml \
       -o -name go.mod -o -name pom.xml -o -name Dockerfile \) \
    -not -path '*/node_modules/*' -print -quit 2>/dev/null)"
  if [ -n "$MANIFEST" ]; then PROJECT_ROOT="$(dirname "$MANIFEST")"; fi
fi
echo "$PROJECT_ROOT" > /out/project_root

# --- 2. Detect stack ------------------------------------------------------
emit detect
STACK="unknown"
if   [ -e "$PROJECT_ROOT/package.json" ]; then STACK="node"
elif [ -e "$PROJECT_ROOT/requirements.txt" ] || [ -e "$PROJECT_ROOT/pyproject.toml" ]; then STACK="python"
elif [ -e "$PROJECT_ROOT/go.mod" ]; then STACK="go"
elif [ -e "$PROJECT_ROOT/pom.xml" ] || [ -e "$PROJECT_ROOT/build.gradle" ]; then STACK="java"
fi
echo "$STACK" > /out/stack

# --- 3. Light, fail-soft syntax check (NO dependency install) -------------
emit validate
( cd "$PROJECT_ROOT"
  case "$STACK" in
    node)
      # node --check each plain JS/MJS file (TS needs a compiler+deps; skip).
      find . -path ./node_modules -prune -o \( -name '*.js' -o -name '*.mjs' -o -name '*.cjs' \) -print \
        | while read -r f; do node --check "$f" || echo "syntax: $f"; done
      ;;
    python)
      find . -name '*.py' -not -path '*/.venv/*' -print \
        | while read -r f; do python -m py_compile "$f" 2>&1 || echo "syntax: $f"; done
      ;;
    go)
      command -v gofmt >/dev/null 2>&1 && gofmt -l . 2>&1 || true
      ;;
    *) echo "stack=$STACK: skipped syntax check" ;;
  esac
) > /out/validate.log 2>&1
echo "$?" > /out/validate_exit

# --- 4. Guarantee a Dockerfile (synthesize if the agent forgot) -----------
emit dockerfile
if [ ! -f "$PROJECT_ROOT/Dockerfile" ]; then
  echo "synthesized" > /out/dockerfile_synth
  case "$STACK" in
    node)
      cat > "$PROJECT_ROOT/Dockerfile" <<'DF'
FROM node:22-slim
WORKDIR /app
COPY package*.json ./
RUN npm install --omit=dev || npm install
COPY . .
ENV PORT=8080
EXPOSE 8080
CMD ["sh","-c","npm start || node dist/index.js || node index.js || node src/index.js"]
DF
      ;;
    python)
      cat > "$PROJECT_ROOT/Dockerfile" <<'DF'
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt* pyproject.toml* ./
RUN pip install --no-cache-dir -r requirements.txt 2>/dev/null || pip install --no-cache-dir . 2>/dev/null || true
COPY . .
ENV PORT=8080
EXPOSE 8080
CMD ["sh","-c","uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} || python main.py || python app.py"]
DF
      ;;
    go)
      cat > "$PROJECT_ROOT/Dockerfile" <<'DF'
FROM golang:1.22 AS build
WORKDIR /app
COPY . .
RUN go build -o /app/server ./... 2>/dev/null || go build -o /app/server .
FROM gcr.io/distroless/base-debian12
COPY --from=build /app/server /server
ENV PORT=8080
EXPOSE 8080
CMD ["/server"]
DF
      ;;
    *)
      cat > "$PROJECT_ROOT/Dockerfile" <<'DF'
# Synthesized fallback Dockerfile — stack not auto-detected.
FROM debian:stable-slim
WORKDIR /app
COPY . .
ENV PORT=8080
EXPOSE 8080
CMD ["sh","-c","echo 'no runnable entrypoint detected' && sleep 3600"]
DF
      ;;
  esac
else
  echo "agent" > /out/dockerfile_synth
fi

# --- 5. Collect deliverable (source only) ---------------------------------
if [ -d "$PROJECT_ROOT" ]; then
  ( cd "$PROJECT_ROOT" && tar \
      --exclude='./node_modules' --exclude='./.git' --exclude='./.venv' \
      --exclude='./vendor' --exclude='./target' --exclude='./dist' \
      --exclude='./*.log' -cf - . ) | ( cd /out/project && tar -xf - )
fi
"""


# Runs INSIDE the be-agent container for the DEPLOY-TIME repair round. The staged
# (failed-to-build) project is mounted at /out; the repair prompt (template +
# build log) is piped over stdin — NOT written into /out — so it never pollutes
# the docker build context. claude edits the project in place; the deploy step
# then re-runs ``docker build``.
_REPAIR_SCRIPT = r"""
export HOME=/home/node
export PATH="$HOME/bin:$PATH"
cd /out
claude -p --output-format stream-json --verbose --permission-mode bypassPermissions --allowedTools Read Write Edit Bash || true
"""


class BackendProjectService:
    """Generate a runnable multi-file backend project via a sandboxed agent."""

    def __init__(self):
        self.image = os.getenv("BE_AGENT_IMAGE", os.getenv("FE_AGENT_IMAGE", "fe-agent:latest"))
        self.docker = os.getenv("DOCKER_BIN", "docker")
        self.gen_timeout = int(os.getenv("BE_AGENT_TIMEOUT", "900"))
        self.total_timeout = int(os.getenv("BE_AGENT_TOTAL_TIMEOUT", "2400"))
        # Deploy-time build self-healing: each AI repair round runs on the staged
        # source after a failed ``docker build`` / container start. The be-agent
        # image now carries real build toolchains (JDK+Maven / Go / Python / Node)
        # so the round COMPILES its edits in-container before handing back; that
        # download+build is slower than a blind edit, hence the wider default.
        self.repair_timeout = int(os.getenv("BE_AGENT_REPAIR_TIMEOUT", "900"))

    # --- prompt assembly -----------------------------------------------------
    def _load_prompt(self, name: str) -> str:
        return prompt_store.get(f"code/{name}")

    @staticmethod
    def _fill(template: str, **values: str) -> str:
        out = template
        for key, value in values.items():
            out = out.replace(f"[[{key}]]", value if value is not None else "")
        return out

    @staticmethod
    def _extract_json(text: str) -> Optional[dict]:
        if not text:
            return None
        cleaned = text.strip()
        fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL | re.IGNORECASE)
        if fenced:
            cleaned = fenced.group(1).strip()
        if not cleaned.startswith("{"):
            match = re.search(r"(\{.*\})", cleaned, re.DOTALL)
            if match:
                cleaned = match.group(1)
        try:
            value = json.loads(cleaned)
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            logger.warning("backend review: failed to parse model JSON output")
            return None

    def is_configured(self) -> bool:
        return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY"))

    def review_project(
        self, *, source_digest: str, contract_summary: str, on_model_call=None
    ) -> Optional[dict]:
        """Acceptance review of the generated backend against the shared contract.

        One text-model call -> ``{verdict, endpoint_coverage, issues, summary}``.
        Advisory: returns ``None`` when no provider is configured or on any
        failure (never raises, never blocks publish)."""
        from backend.services.ai import get_text_provider

        provider = get_text_provider()
        if not provider or not provider.is_configured():
            return None
        try:
            template = self._load_prompt("backend_project_critic_prompt.txt")
        except Exception:  # noqa: BLE001
            return None
        prompt = self._fill(template, CONTRACT=contract_summary or "", SOURCE=source_digest or "")
        provider_name = getattr(provider, "provider_name", None)
        model_name = getattr(provider, "model", None)
        try:
            result = provider.generate_text(prompt)
        except Exception as error:  # noqa: BLE001
            logger.warning("backend review model call raised: %s", error)
            if on_model_call:
                on_model_call(prompt=prompt, text=None, success=False, error=str(error),
                              provider=provider_name, model=model_name)
            return None
        if on_model_call:
            on_model_call(prompt=prompt, text=result.text if result.success else None,
                          success=result.success, error=result.error,
                          provider=provider_name, model=model_name)
        if not result.success:
            return None
        return self._extract_json(result.text)

    # --- public API ----------------------------------------------------------
    def build_project(
        self,
        *,
        requirement: str,
        requirements_doc: str = "",
        development_flow: str = "",
        documents_digest: str = "",
        contract_block: str = "",
        middleware_block: str = "",
        context_ledger: str = "",
        on_event: Optional[Callable[[dict], None]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> dict:
        """Run the containerized agent to produce the backend project source.

        Returns ``{success, error, files, stack, dockerfile_source, summary,
        usage, cost_usd, workdir}``. Never raises on agent/model failure (returns
        ``success=False``); raises only on infrastructure errors.
        """
        if not self.is_configured():
            return self._empty("ANTHROPIC_API_KEY not configured")

        _cap = 16_000
        prompt = self._fill(
            self._load_prompt("backend_project_prompt.txt"),
            CONTEXT_LEDGER=context_ledger or "",
            REQUIREMENT=(requirement or "")[:4_000],
            REQUIREMENTS_DOC=(requirements_doc or "")[:_cap],
            DEVELOPMENT_FLOW=(development_flow or "")[:_cap],
            DOCUMENTS=documents_digest or "",
            CONTRACT=contract_block or "",
            MIDDLEWARE=middleware_block or "",
        )

        workdir = Path(tempfile.mkdtemp(prefix="be-agent-"))
        os.chmod(workdir, 0o777)
        (workdir / "prompt.txt").write_text(prompt, encoding="utf-8")
        api_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY")

        cmd = [
            self.docker, "run", "--rm", "--user", "node",
            "-e", "ANTHROPIC_API_KEY",
            "-e", "OPENAI_API_KEY",
            "-e", f"BE_AGENT_TIMEOUT={self.gen_timeout}",
            "-v", f"{workdir}:/out", self.image, "bash", "-c", _CONTAINER_SCRIPT,
        ]
        env = dict(os.environ, ANTHROPIC_API_KEY=api_key or "")
        openai_key = os.getenv("OPENAI_API_KEY")
        if openai_key:
            env["OPENAI_API_KEY"] = openai_key

        result_events: list[dict] = []
        non_json_stdout: list[str] = []
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
                    non_json_stdout.append(line[:500])
                    non_json_stdout = non_json_stdout[-20:]
                    continue
                if event.get("type") == "result":
                    result_events.append(event)
                if on_event:
                    try:
                        on_event(event)
                    except Exception:  # noqa: BLE001
                        logger.exception("on_event callback raised")
                if is_cancelled and is_cancelled():
                    proc.kill()
                    return self._empty("cancelled")
            proc.wait(timeout=self.total_timeout if self.total_timeout > 0 else None)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise RuntimeError("backend project agent timed out")
        finally:
            stderr = proc.stderr.read() if proc.stderr else ""
            if stderr:
                logger.info("be-agent stderr: %s", stderr[:2000])

        docker_exit = proc.returncode
        claude_exit = self._read_exit_code(workdir / "claude_exit")
        stack = self._read_text(workdir / "stack").strip() or "unknown"
        dockerfile_origin = self._read_text(workdir / "dockerfile_synth").strip() or "unknown"
        validate_log = self._read_text(workdir / "validate.log")
        files = self._collect(workdir / "project")

        summary, usage, cost, is_error = "", {}, 0.0, True
        if result_events:
            summary = next(
                (r.get("result") for r in result_events if isinstance(r.get("result"), str)), ""
            )
            cost = sum(float(r.get("total_cost_usd") or 0.0) for r in result_events)
            usage = self._merge_usage(result_events)
            is_error = bool(result_events[-1].get("is_error"))

        has_dockerfile = any(Path(k).name == "Dockerfile" for k in files)
        success = (docker_exit == 0) and bool(files) and has_dockerfile
        dockerfile_source = files.get("Dockerfile")
        return {
            "success": success,
            "error": None if success else self._format_failure(
                docker_exit=docker_exit, claude_exit=claude_exit, is_error=is_error,
                files=files, has_dockerfile=has_dockerfile, summary=summary,
                stderr=stderr, non_json_stdout=non_json_stdout, validate_log=validate_log,
            ),
            "files": files,
            "stack": stack,
            "dockerfile_origin": dockerfile_origin,  # 'agent' | 'synthesized'
            "dockerfile_source": (
                dockerfile_source.decode("utf-8", "replace")
                if isinstance(dockerfile_source, (bytes, bytearray)) else dockerfile_source
            ),
            "validate_log": validate_log[:4000],
            "summary": summary,
            "usage": usage,
            "cost_usd": cost,
            "workdir": str(workdir),
        }

    def repair_build(
        self,
        *,
        workdir: str,
        build_log: str,
        on_log: Optional[Callable[[str], None]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> dict:
        """AI self-heal a backend project whose ``docker build`` / startup failed.

        Runs the agent in-place on the staged source (mounted at ``/out``): claude
        reads the build log + Dockerfile + source and edits files to make the
        project buildable/startable. The deploy step then re-runs ``docker build``.
        The prompt is piped over stdin (no build-context pollution). Returns
        ``{ran, summary, cost_usd}``; ``ran`` is False when no provider is
        configured (deploy then skips the repair rung).
        """
        if not self.is_configured():
            return {"ran": False, "summary": "", "cost_usd": 0.0, "error": "ANTHROPIC_API_KEY not configured"}
        wd = Path(workdir)
        if not wd.is_dir():
            return {"ran": False, "summary": "", "cost_usd": 0.0, "error": "workdir missing"}
        # The container runs as the non-root ``node`` uid; make the staged tree
        # writable so claude can edit the existing (backend-written) files.
        self._make_writable(wd)
        prompt = self._load_prompt("backend_project_repair_prompt.txt") + "\n\n" + (build_log or "")[-8000:]
        api_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY")
        cmd = [
            self.docker, "run", "--rm", "-i", "--user", "node",
            "-e", "ANTHROPIC_API_KEY", "-e", "OPENAI_API_KEY",
            "-v", f"{wd}:/out", self.image, "bash", "-c", _REPAIR_SCRIPT,
        ]
        env = dict(os.environ, ANTHROPIC_API_KEY=api_key or "")
        openai_key = os.getenv("OPENAI_API_KEY")
        if openai_key:
            env["OPENAI_API_KEY"] = openai_key

        result_events: list[dict] = []
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env,
        )
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
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
                if on_log:
                    try:
                        self._repair_event_to_log(event, on_log)
                    except Exception:  # noqa: BLE001
                        pass
                if is_cancelled and is_cancelled():
                    proc.kill()
                    return {"ran": True, "summary": "cancelled", "cost_usd": 0.0}
            proc.wait(timeout=self.repair_timeout if self.repair_timeout > 0 else None)
        except subprocess.TimeoutExpired:
            proc.kill()
            return {"ran": True, "summary": "repair timed out", "cost_usd": 0.0}
        finally:
            if proc.stderr:
                err = proc.stderr.read()
                if err:
                    logger.info("be-agent repair stderr: %s", err[:1500])

        summary, cost = "", 0.0
        if result_events:
            summary = next(
                (r.get("result") for r in result_events if isinstance(r.get("result"), str)), ""
            )
            cost = sum(float(r.get("total_cost_usd") or 0.0) for r in result_events)
        return {"ran": True, "summary": summary, "cost_usd": cost}

    @staticmethod
    def _make_writable(root: Path) -> None:
        try:
            os.chmod(root, 0o777)
        except OSError:
            pass
        for path in root.rglob("*"):
            try:
                os.chmod(path, 0o777 if path.is_dir() else 0o666)
            except OSError:
                continue

    @staticmethod
    def _repair_event_to_log(event: dict, on_log: Callable[[str], None]) -> None:
        etype = event.get("type")
        if etype == "assistant":
            for block in event.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = block.get("name") or ""
                    inp = block.get("input") or {}
                    if name in ("Write", "Edit"):
                        fpath = inp.get("file_path") or inp.get("path") or ""
                        on_log(f"AI 修复:写入 {fpath}")
        elif etype == "result":
            text = event.get("result")
            if isinstance(text, str) and text:
                on_log(f"AI 修复完成:{text[:120]}")

    # --- helpers -------------------------------------------------------------
    @staticmethod
    def _empty(error: str) -> dict:
        return {
            "success": False, "error": error, "files": {}, "stack": "unknown",
            "dockerfile_origin": None, "dockerfile_source": None, "validate_log": "",
            "summary": "", "usage": {}, "cost_usd": 0.0, "workdir": None,
        }

    @staticmethod
    def _merge_usage(events: list[dict]) -> dict:
        merged: dict = {}
        for event in events:
            usage = event.get("usage") or {}
            for key, value in usage.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    merged[key] = merged.get(key, 0) + value
                elif key not in merged:
                    merged[key] = value
        return merged

    @staticmethod
    def _read_exit_code(path: Path) -> int | None:
        try:
            raw = path.read_text(encoding="utf-8").strip()
            return int(raw) if raw else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    @staticmethod
    def _clip(text: str, limit: int = 1200) -> str:
        text = (text or "").strip()
        return text if len(text) <= limit else f"{text[:limit]}..."

    def _format_failure(
        self, *, docker_exit, claude_exit, is_error, files, has_dockerfile,
        summary, stderr, non_json_stdout, validate_log,
    ) -> str:
        reasons: list[str] = []
        if docker_exit not in (0, None):
            reasons.append(f"container exited with code {docker_exit}")
        if claude_exit not in (0, None):
            reasons.append(f"claude exited with code {claude_exit}")
        if is_error:
            reasons.append("claude reported an error")
        if not files:
            reasons.append("no source files were published")
        if files and not has_dockerfile:
            reasons.append("no Dockerfile was produced")
        details: list[str] = []
        if summary:
            details.append(f"summary: {self._clip(summary)}")
        if stderr:
            details.append(f"stderr: {self._clip(stderr)}")
        if non_json_stdout:
            details.append(f"stdout: {self._clip(chr(10).join(non_json_stdout))}")
        if validate_log:
            details.append(f"validate: {self._clip(validate_log)}")
        headline = "; ".join(reasons) or "agent did not produce a backend project"
        return " | ".join([headline, *details])

    def _collect(self, root: Path) -> dict:
        """Return ``{relative_path: bytes}`` for files under ``root`` (binary-safe)."""
        out: dict[str, bytes] = {}
        if not root.exists():
            return out
        asset_budget = _MAX_TOTAL_ASSET_BYTES
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            is_asset = path.suffix.lower() in _BINARY_EXTS
            cap = _MAX_ASSET_BYTES if is_asset else _MAX_TEXT_BYTES
            try:
                size = path.stat().st_size
                if size > cap:
                    logger.warning("skip %s: %d bytes exceeds cap", rel, size)
                    continue
                if is_asset:
                    if size > asset_budget:
                        continue
                    asset_budget -= size
                out[str(rel)] = path.read_bytes()
            except OSError:
                continue
        return out


_service_instance: Optional[BackendProjectService] = None


def get_backend_project_service() -> BackendProjectService:
    global _service_instance
    if _service_instance is None:
        _service_instance = BackendProjectService()
    return _service_instance
