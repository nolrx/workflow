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

from backend.services.code.docker_env import (
    ANTHROPIC_RETRY_PROXY_BOOTSTRAP,
    anthropic_agent_credentials,
    anthropic_configured,
    container_user,
    host_workdir,
    mount_failure_hint,
)
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
export HOME="${HOME:-/home/node}"
WORK=/tmp/work
mkdir -p "$WORK" && cd "$WORK"
# Iteration (二次开发) seed: when an existing project source was provided at
# /out/_base, copy it into the workdir so the agent EDITS it in place (真实续改)
# instead of generating from scratch. No-op (identical to fresh generation) when
# /out/_base is absent. Removed afterwards so it is never tarred into the output.
if [ -d /out/_base ] && [ -n "$(ls -A /out/_base 2>/dev/null)" ]; then
  cp -a /out/_base/. "$WORK"/ 2>/dev/null || true
  rm -rf /out/_base
  echo seeded > /out/seeded
fi
export CODEX_HOME="$HOME/.codex"
export PATH="$HOME/bin:$PATH"
mkdir -p "$HOME/bin" "$CODEX_HOME"

CLAUDE_FLAGS="--output-format stream-json --verbose --permission-mode bypassPermissions --allowedTools Read Write Edit Bash"

# __ANTHROPIC_PROXY_BOOTSTRAP__

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

# --- 1b. BMAD reinforce pass (functional completeness) --------------------
# A SECOND autonomous-CLI pass over the just-generated project: turn every
# FR/NFR/M anchor into a REAL end-to-end implementation (state machines /
# server-side validation / async & scheduled jobs / AI call-chains) and kill
# stubs/TODOs. NO docker, NO contract changes. Runs BEFORE the build ladder so
# install/compile/test verify the REINFORCED tree. Prompt over stdin (E2BIG-safe);
# the CLI runs INSIDE PROJECT_ROOT so it edits the generated files. Gated by
# BE_AGENT_REINFORCE (default 1) AND a non-empty reinforce prompt (fail-soft skip).
if [ "${BE_AGENT_REINFORCE:-1}" != "0" ] && [ -s /out/reinforce_prompt.txt ]; then
  emit reinforce
  ( cd "$PROJECT_ROOT" && run_capped "${BE_AGENT_REINFORCE_TIMEOUT:-${BE_AGENT_TIMEOUT:-900}}" \
      claude -p $CLAUDE_FLAGS < /out/reinforce_prompt.txt )
  echo "$?" > /out/reinforce_exit
  # The reinforce agent may have created the manifest only on this pass; re-resolve
  # PROJECT_ROOT the SAME way as after first-gen so detect/build see the right dir.
  if [ ! -e "$PROJECT_ROOT/package.json" ] && [ ! -e "$PROJECT_ROOT/requirements.txt" ] \
     && [ ! -e "$PROJECT_ROOT/pyproject.toml" ] && [ ! -e "$PROJECT_ROOT/go.mod" ] \
     && [ ! -e "$PROJECT_ROOT/pom.xml" ] && [ ! -e "$PROJECT_ROOT/Dockerfile" ]; then
    MANIFEST2="$(find "$WORK" -maxdepth 3 \
      \( -name package.json -o -name requirements.txt -o -name pyproject.toml \
         -o -name go.mod -o -name pom.xml -o -name Dockerfile \) \
      -not -path '*/node_modules/*' -print -quit 2>/dev/null)"
    if [ -n "$MANIFEST2" ]; then PROJECT_ROOT="$(dirname "$MANIFEST2")"; echo "$PROJECT_ROOT" > /out/project_root; fi
  fi
else
  echo "skipped" > /out/reinforce_exit
fi

# --- 2. Detect stack ------------------------------------------------------
emit detect
STACK="unknown"
if   [ -e "$PROJECT_ROOT/package.json" ]; then STACK="node"
elif [ -e "$PROJECT_ROOT/requirements.txt" ] || [ -e "$PROJECT_ROOT/pyproject.toml" ]; then STACK="python"
elif [ -e "$PROJECT_ROOT/go.mod" ]; then STACK="go"
elif [ -e "$PROJECT_ROOT/pom.xml" ] || [ -e "$PROJECT_ROOT/build.gradle" ]; then STACK="java"
fi
echo "$STACK" > /out/stack

# --- 3. Real native build + test + package-verify + AI self-heal ladder ----
# The be-agent image carries full toolchains (JDK+Maven / Go / Python / Node) but
# has NO docker.sock — so "verify it can be packaged" here means a REAL native
# build: deps resolve + compile + tests pass + a packageable artifact. The actual
# docker image packaging happens at DEPLOY time on the platform (which holds the
# socket). Mirrors frontend_project_service's self-healing ladder, for polyglot
# backends. Logs go to /out/*.log (NEVER -q/quiet: the full log is the only clue
# the AI repair has).
cd "$PROJECT_ROOT"

# scaffold-check: prefer the project's OWN unified entrypoint (Makefile targets /
# ci.sh) so generation, deploy-time repair and external CI all run the SAME
# commands — judgement never drifts. Else fall back to stack-native commands.
SCAFFOLD="none"
if [ -f Makefile ] && grep -qE '^(build|test)[[:space:]]*:' Makefile 2>/dev/null; then SCAFFOLD="make"
elif [ -f ci.sh ]; then SCAFFOLD="ci"; fi
echo "$SCAFFOLD" > /out/scaffold

# Each TEST_CMD records a DETERMINISTIC tests-ran marker (/out/tests_ran_marker:
# 1 = a real test command was attempted, 0 = an explicit skip). NOT inferred from
# log text — the log's first line echoes the command string, so any substring
# heuristic would self-pollute (e.g. the word "skipping" in the skip branch).
case "$STACK" in
  node)
    NI='npm ci || npm install --no-audit --no-fund'
    NC='if [ -f tsconfig.json ]; then npx --no-install tsc --noEmit; else npm run build --if-present; fi'
    NT='if grep -q "no test specified" package.json 2>/dev/null; then echo 0 > /out/tests_ran_marker; echo "default npm test placeholder; not run"; exit 0; else echo 1 > /out/tests_ran_marker; npm test --if-present; fi'
    ;;
  python)
    NI='python3 -m venv .venv && . .venv/bin/activate && { if [ -f requirements.txt ]; then pip install -r requirements.txt; elif [ -f pyproject.toml ]; then pip install -e .; else echo "no python deps file"; fi; }'
    NC='. .venv/bin/activate 2>/dev/null; python -m compileall -x "(\.venv|node_modules)" .'
    NT='. .venv/bin/activate 2>/dev/null; if python -c "import pytest" 2>/dev/null; then echo 1 > /out/tests_ran_marker; pytest; rc=$?; [ "$rc" = "5" ] && rc=0; exit $rc; else echo 0 > /out/tests_ran_marker; echo "no pytest; not run"; exit 0; fi'
    ;;
  go)
    NI='go mod download'
    NC='go build ./...'
    NT='echo 1 > /out/tests_ran_marker; go test ./...'
    ;;
  java)
    NI='mvn -B -e -DskipTests dependency:resolve'
    NC='mvn -B -e -DskipTests compile'
    NT='echo 1 > /out/tests_ran_marker; mvn -B -e test'
    ;;
  *)
    NI='echo "stack unknown: no install"'; NC='echo "stack unknown: no compile"'
    NT='echo 0 > /out/tests_ran_marker; echo "stack unknown: no tests"'
    ;;
esac

mk() { [ "$SCAFFOLD" = "make" ] && grep -qE "^$1[[:space:]]*:" Makefile 2>/dev/null; }
INSTALL_CMD="$NI"; COMPILE_CMD="$NC"; TEST_CMD="$NT"; PACKAGE_CMD='true'
if mk install; then INSTALL_CMD='make install'; fi
if mk build;   then COMPILE_CMD='make build'; fi
if mk test;    then TEST_CMD='echo 1 > /out/tests_ran_marker; make test'; fi
if mk package; then PACKAGE_CMD='make package'; fi
if [ "$SCAFFOLD" = "ci" ]; then
  INSTALL_CMD='echo "install covered by ci.sh"'; COMPILE_CMD='bash ci.sh'
  TEST_CMD='echo 1 > /out/tests_ran_marker; echo "tests covered by ci.sh"'
fi

run_phase() {  # run_phase <timeout-seconds> <logfile> <command-string>; returns the cmd exit
  local secs="$1" logf="$2" cmd="$3"
  case "$secs" in (''|*[!0-9]*) secs=0 ;; esac
  printf '$ %s\n' "$cmd" > "$logf"
  if [ "$secs" -eq 0 ]; then bash -c "$cmd" >> "$logf" 2>&1
  else timeout "$secs" bash -c "$cmd" >> "$logf" 2>&1; fi
  return $?
}

INSTALL_T="${BE_AGENT_INSTALL_TIMEOUT:-480}"
COMPILE_T="${BE_AGENT_BUILD_TIMEOUT_GEN:-480}"
TEST_T="${BE_AGENT_TEST_TIMEOUT:-300}"
PKG_T="${BE_AGENT_PACKAGE_TIMEOUT:-180}"
GEN_REPAIRS="${BE_AGENT_GEN_REPAIRS:-1}"
case "$GEN_REPAIRS" in (''|*[!0-9]*) GEN_REPAIRS=1 ;; esac

BUILD_STATE=""; attempt=0
while : ; do
  emit install;        run_phase "$INSTALL_T" /out/install.log "$INSTALL_CMD"; install_exit=$?
  emit compile;        run_phase "$COMPILE_T" /out/compile.log "$COMPILE_CMD"; compile_exit=$?
  emit test;           run_phase "$TEST_T"    /out/test.log    "$TEST_CMD";    test_exit=$?
  emit package-verify; run_phase "$PKG_T"     /out/package.log "$PACKAGE_CMD"; :
  echo "$install_exit" > /out/install_exit
  echo "$compile_exit" > /out/compile_exit
  echo "$test_exit"    > /out/test_exit
  if [ "$install_exit" -eq 0 ] && [ "$compile_exit" -eq 0 ] && [ "$test_exit" -eq 0 ]; then
    if [ "$attempt" -eq 0 ]; then BUILD_STATE="green"; else BUILD_STATE="green-repaired"; fi
    break
  fi
  if [ "$attempt" -ge "$GEN_REPAIRS" ]; then
    if   [ "$install_exit" -ne 0 ]; then BUILD_STATE="install-failed"
    elif [ "$compile_exit" -ne 0 ]; then BUILD_STATE="compile-failed"
    else BUILD_STATE="test-failed"; fi
    break
  fi
  attempt=$((attempt + 1))
  # AI self-heal round: feed the repair prompt + the tails of the failed logs
  # over stdin (E2BIG-safe; never written into PROJECT_ROOT or it'd be tarred out).
  emit ai-repair
  { cat /out/repair_prompt.txt
    printf '\n\n# 本机原生构建/测试报错(节选 — 据此修复,目标:make build && make test 全绿)\n'
    printf '\n## install.log\n'; tail -c 2500 /out/install.log 2>/dev/null
    printf '\n## compile.log\n'; tail -c 3000 /out/compile.log 2>/dev/null
    printf '\n## test.log\n';    tail -c 3000 /out/test.log 2>/dev/null
  } > /out/repair_full_prompt.txt
  run_capped "${BE_AGENT_REPAIR_TIMEOUT:-900}" claude -p $CLAUDE_FLAGS < /out/repair_full_prompt.txt
  echo "$?" >> /out/claude_repair_exit
done
# Unknown stack = nothing was actually built/tested (install/compile/test were
# no-op echoes that exit 0). Don't let that masquerade as a verified-green build;
# mark it degraded so the workflow flags it instead of reporting "验证通过".
if [ "$STACK" = "unknown" ]; then
  case "$BUILD_STATE" in green|green-repaired) BUILD_STATE="stack-unknown" ;; esac
fi
echo "$BUILD_STATE" > /out/build_state
echo "$attempt" > /out/repaired_rounds
# /out/degraded: empty when green/green-repaired, else the failed/unverified stage.
case "$BUILD_STATE" in green|green-repaired) : > /out/degraded ;; *) printf '%s' "$BUILD_STATE" > /out/degraded ;; esac

# Dockerfile static lint (advisory only; the synth step below still guarantees a
# Dockerfile exists). Surfaces obvious deploy-time build hazards at gen time.
DF_WARN=""
if [ -f Dockerfile ]; then
  grep -q "EXPOSE 8080" Dockerfile 2>/dev/null || DF_WARN="${DF_WARN}no-EXPOSE-8080; "
  grep -qiE "^[[:space:]]*(CMD|ENTRYPOINT)" Dockerfile 2>/dev/null || DF_WARN="${DF_WARN}no-CMD/ENTRYPOINT; "
  if grep -qE "([[:space:]](-q|--quiet)([[:space:]]|\$))|>[[:space:]]*/dev/null" Dockerfile 2>/dev/null; then DF_WARN="${DF_WARN}build-log-suppressed; "; fi
fi
printf '%s' "$DF_WARN" > /out/dockerfile_warn

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
export HOME="${HOME:-/home/node}"
export PATH="$HOME/bin:$PATH"
# __ANTHROPIC_PROXY_BOOTSTRAP__
cd /out
claude -p --output-format stream-json --verbose --permission-mode bypassPermissions --allowedTools Read Write Edit Bash || true
"""


# Runs INSIDE the be-agent container for the DEPLOY-TIME *comprehensive* repair
# round, driven by the OpenAI Codex CLI (the be-agent image pre-bakes `codex`).
# One Codex pass fixes EVERYTHING observed on the running backend in this round —
# database / data-layer, runtime (5xx) crashes and interface defects — editing the
# staged source mounted at /out in place; the deploy step then re-runs `docker
# build`, swaps the container and re-checks. Two stdin sources are deliberately
# kept apart: the OpenAI key is fed to `codex login` over an ISOLATED pipe (so it
# never consumes the prompt), while the aggregated repair brief arrives on the
# container's OWN stdin (the host pipes it) and is read by `codex exec` — kept off
# argv (E2BIG) AND out of /out (never pollutes the rebuild context / promoted
# source). `exec` replaces the shell so codex's exit code IS the container's.
_CODEX_REPAIR_SCRIPT = r"""
set -u
export HOME="${HOME:-/home/node}"
export CODEX_HOME="$HOME/.codex"
export PATH="$HOME/bin:$PATH"
mkdir -p "$CODEX_HOME"
# Drain the host-piped aggregated brief off stdin into a temp file OUTSIDE /out
# (so it never pollutes the rebuild context / promoted source), THEN feed it to
# codex via an explicit redirect — the proven slicer/fe-agent contract (don't rely
# on codex inheriting the shell's stdin). `cat` reads stdin fully to EOF first, so
# the key pipe below can't race it.
cat > /tmp/repair_prompt.txt
cd /out
# codex (>=0.x) does NOT pick up OPENAI_API_KEY from the environment on its own:
# the key must be written to auth.json via `login --with-api-key` (reads the key
# from ITS OWN stdin pipe — independent of /tmp/repair_prompt.txt above).
printf '%s' "${OPENAI_API_KEY:-}" | codex login --with-api-key >/dev/null 2>&1 || true
# codex edits /out in place. Merge stderr into stdout (2>&1) so the host drains ONE
# pipe — codex's progress can be large and the host reads stderr only after the
# stdout loop, so a separate stderr stream could fill its 64KB pipe and deadlock.
# `exec` makes codex's exit the container's.
exec timeout "${BE_CODEX_REPAIR_TIMEOUT:-900}" codex ${BE_CODEX_REPAIR_FLAGS} < /tmp/repair_prompt.txt 2>&1
"""

# Splice the gateway retry-proxy bootstrap in before the claude calls of the
# generation + deploy-time-repair scripts (both hit the gateway). The Codex repair
# script uses OpenAI, not the Anthropic gateway, so it gets no marker / no-op. See
# docker_env.py for why (flaky gateway random-403 that the claude CLI won't retry).
_CONTAINER_SCRIPT = _CONTAINER_SCRIPT.replace(
    "# __ANTHROPIC_PROXY_BOOTSTRAP__", ANTHROPIC_RETRY_PROXY_BOOTSTRAP
)
_REPAIR_SCRIPT = _REPAIR_SCRIPT.replace(
    "# __ANTHROPIC_PROXY_BOOTSTRAP__", ANTHROPIC_RETRY_PROXY_BOOTSTRAP
)


class BackendProjectService:
    """Generate a runnable multi-file backend project via a sandboxed agent."""

    def __init__(self):
        self.image = os.getenv("BE_AGENT_IMAGE", os.getenv("FE_AGENT_IMAGE", "fe-agent:latest"))
        self.docker = os.getenv("DOCKER_BIN", "docker")
        # Generate phase now writes MORE files (project + Makefile + tests +
        # ARCHITECTURE.md + README + .github CI), so the write budget is wider.
        self.gen_timeout = int(os.getenv("BE_AGENT_TIMEOUT", "1200"))
        # Per-phase budgets for the generation-time REAL build ladder (install ->
        # compile -> test -> package-verify), all guarded by in-container timeouts.
        self.install_timeout = int(os.getenv("BE_AGENT_INSTALL_TIMEOUT", "480"))
        self.build_timeout_gen = int(os.getenv("BE_AGENT_BUILD_TIMEOUT_GEN", "480"))
        self.test_timeout = int(os.getenv("BE_AGENT_TEST_TIMEOUT", "300"))
        self.package_timeout = int(os.getenv("BE_AGENT_PACKAGE_TIMEOUT", "180"))
        # Generation-time AI self-heal rounds after a red native build/test. Kept
        # to 1 by default: one round covers most serial compile errors, and deploy
        # has a SECOND ladder (APP_BUILD_REPAIRS) as backstop. Ops can raise it.
        self.gen_repairs = int(os.getenv("BE_AGENT_GEN_REPAIRS", "1"))
        # Host-side backstop on the WHOLE generate run. Must comfortably exceed the
        # SUM of every in-container phase cap (gen + install+compile+test+package +
        # N*(repair+install+compile+test+package)); else the host kills the
        # container mid-build, the tar never runs, files come back empty and the
        # run is wrongly refunded. With REPAIRS=1 the worst case is ~5.7k s, so the
        # default is generous; compose sets it to 0 (wait forever — per-phase caps
        # are the real guard) to fully remove the false-kill window, mirroring fe.
        # The BMAD reinforce pass (below) adds a second full generation, so the
        # bare-metal worst case grows to ~7.2k s (REPAIRS=1 + REINFORCE=1); hence
        # the generous default (compose still sets 0 = run to completion).
        self.total_timeout = int(os.getenv("BE_AGENT_TOTAL_TIMEOUT", "9000"))
        # BMAD second pass (functional completeness): after the first generation,
        # before the native build ladder, a second claude pass turns every FR/NFR/M
        # functional anchor into a real end-to-end implementation (no stubs). On by
        # default; ops disable with BE_AGENT_REINFORCE=0. Its budget reuses the gen
        # budget unless BE_AGENT_REINFORCE_TIMEOUT is set. NOTE: resolve "0 = reuse
        # gen" HERE (not in the container) — the value is always passed via -e, so a
        # literal 0 in the container would defeat the shell ${VAR:-fallback} and run
        # the reinforce pass UNCAPPED on bare-metal (where gen is finite). With this,
        # the passed value is the real effective cap: gen_timeout on bare-metal, or 0
        # (run to completion) under compose where gen_timeout is itself 0.
        self.reinforce_enabled = os.getenv("BE_AGENT_REINFORCE", "1")  # passed verbatim to the container
        self.reinforce_timeout = int(os.getenv("BE_AGENT_REINFORCE_TIMEOUT", "0")) or self.gen_timeout
        # Deploy-time build self-healing: each AI repair round runs on the staged
        # source after a failed ``docker build`` / container start. The be-agent
        # image now carries real build toolchains (JDK+Maven / Go / Python / Node)
        # so the round COMPILES its edits in-container before handing back; that
        # download+build is slower than a blind edit, hence the wider default. The
        # generation-time ladder reuses this same per-round budget.
        self.repair_timeout = int(os.getenv("BE_AGENT_REPAIR_TIMEOUT", "900"))
        # Deploy-time COMPREHENSIVE repair via the OpenAI Codex CLI (the be-agent
        # image pre-bakes `codex`). One Codex pass fixes database + runtime + interface
        # against the running container, then the deploy step rebuilds + re-checks.
        # Separate timeout/flags from the Claude rungs; `exec` reads the prompt off
        # stdin (no positional, no --json so the plain progress streams to on_log).
        self.codex_repair_timeout = int(os.getenv("BE_CODEX_REPAIR_TIMEOUT", "900"))
        self.codex_repair_flags = os.getenv(
            "BE_CODEX_REPAIR_FLAGS",
            "exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check",
        )

    # --- prompt assembly -----------------------------------------------------
    def _load_prompt(self, name: str) -> str:
        return prompt_store.get(f"code/{name}")

    @staticmethod
    def _fill(template: str, **values: str) -> str:
        out = template
        for key, value in values.items():
            out = out.replace(f"[[{key}]]", value if value is not None else "")
        return out

    def _build_prompt(
        self, fill_vals: dict, edit_mode: bool, base_files: dict,
        change_instruction: str, change_plan: str,
    ) -> str:
        """Pick + fill the generation prompt (edit-mode variant for 二次开发续改)."""
        if not edit_mode:
            return self._fill(self._load_prompt("backend_project_prompt.txt"), **fill_vals)
        base_list = "\n".join(f"- {p}" for p in sorted(base_files)[:300])
        edit_vals = dict(
            fill_vals,
            CHANGE_INSTRUCTION=(change_instruction or "")[:4_000],
            CHANGE_PLAN=(change_plan or "")[:4_000],
            BASE_FILES=base_list,
        )
        try:
            return self._fill(self._load_prompt("backend_project_edit_prompt.txt"), **edit_vals)
        except Exception:  # noqa: BLE001 — edit template missing: prepend a续改 preamble
            base = self._fill(self._load_prompt("backend_project_prompt.txt"), **fill_vals)
            return (
                "【二次开发·基于现有项目续改】当前目录已是现有后端工程,请只针对下述变更"
                "修改/新增相关文件,保持其余文件不变,不要整体重写。\n"
                f"变更要求:{(change_instruction or '')[:4000]}\n执行计划:\n{(change_plan or '')[:4000]}\n\n"
                + base
            )

    @staticmethod
    def _seed_base(base_dir: "Path", files: dict) -> None:
        """Write the existing project source under workdir/_base (binary-safe).

        Hardened against path traversal: a crafted zip member name like
        ``../../x`` must not escape ``base_dir`` and write onto the host. Absolute
        paths, ``..`` segments, and anything that resolves outside base_dir are
        skipped (the source zips come from agent output — defence in depth).
        """
        base_resolved = base_dir.resolve()
        written = 0
        for rel, content in files.items():
            if written > 4000:  # backstop against a pathological file count
                break
            norm = str(rel).replace("\\", "/")
            if not norm or norm.startswith("/") or ".." in norm.split("/"):
                continue
            try:
                dest = base_dir / norm
                resolved = dest.resolve()
                if base_resolved != resolved and base_resolved not in resolved.parents:
                    continue  # escaped base_dir → skip
                dest.parent.mkdir(parents=True, exist_ok=True)
                data = content if isinstance(content, (bytes, bytearray)) else str(content).encode("utf-8")
                dest.write_bytes(bytes(data))
                written += 1
            except Exception:  # noqa: BLE001 — skip any single bad path, keep seeding
                continue

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
        return anthropic_configured()

    @staticmethod
    def is_codex_configured() -> bool:
        """Whether the OpenAI Codex CLI repair path can run (its own key, separate
        from the Claude rungs). ``repair_5xx`` returns ``ran=False`` when unset so the
        deploy's comprehensive-repair ladder degrades to best-effort-proceed."""
        return bool(os.getenv("OPENAI_API_KEY"))

    def review_project(
        self, *, source_digest: str, contract_summary: str,
        requirements_doc: str = "", development_flow: str = "", on_model_call=None
    ) -> Optional[dict]:
        """Acceptance review of the generated backend against the shared contract.

        One text-model call -> ``{verdict, endpoint_coverage, issues, summary}``.
        ``requirements_doc`` / ``development_flow`` inject the canonical FR/NFR/M
        anchor lists so the critic can do real traceability (the contract alone
        may not preserve anchor numbering). Advisory: returns ``None`` when no
        provider is configured or on any failure (never raises, never blocks
        publish)."""
        from backend.services.ai import get_text_provider

        provider = get_text_provider()
        if not provider or not provider.is_configured():
            return None
        try:
            template = self._load_prompt("backend_project_critic_prompt.txt")
        except Exception:  # noqa: BLE001
            return None
        _cap = 24000
        prompt = self._fill(
            template,
            CONTRACT=contract_summary or "",
            REQUIREMENTS_DOC=(requirements_doc or "")[:_cap],
            DEVELOPMENT_FLOW=(development_flow or "")[:_cap],
            SOURCE=source_digest or "",
        )
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
        base_files: Optional[dict] = None,
        change_instruction: str = "",
        change_plan: str = "",
        on_event: Optional[Callable[[dict], None]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> dict:
        """Run the containerized agent to produce the backend project source.

        ``base_files`` (二次开发 iteration): when provided, the existing project
        source is seeded into the container so the agent EDITS it per
        ``change_instruction`` / ``change_plan`` instead of generating from
        scratch (真实续改). The reinforce pass is skipped in edit mode so it can't
        rewrite the inherited project.

        Returns ``{success, error, files, stack, dockerfile_source, summary,
        usage, cost_usd, workdir}``. Never raises on agent/model failure (returns
        ``success=False``); raises only on infrastructure errors.
        """
        if not self.is_configured():
            return self._empty("ANTHROPIC_API_KEY not configured")
        edit_mode = bool(base_files)

        _cap = 16_000
        # Shared anchor injection for BOTH the first-gen prompt and the reinforce
        # prompt (same FR/NFR/M anchors), so the two passes can't drift on inputs.
        fill_vals = dict(
            CONTEXT_LEDGER=context_ledger or "",
            REQUIREMENT=(requirement or "")[:4_000],
            REQUIREMENTS_DOC=(requirements_doc or "")[:_cap],
            DEVELOPMENT_FLOW=(development_flow or "")[:_cap],
            DOCUMENTS=documents_digest or "",
            CONTRACT=contract_block or "",
            MIDDLEWARE=middleware_block or "",
        )
        prompt = self._build_prompt(
            fill_vals, edit_mode, base_files or {}, change_instruction, change_plan
        )

        workdir = Path(tempfile.mkdtemp(prefix="be-agent-"))
        os.chmod(workdir, 0o777)
        (workdir / "prompt.txt").write_text(prompt, encoding="utf-8")
        # Edit mode: seed the existing project source so the container copies it
        # into the agent workdir (真实续改). Binary-safe; skips pathological files.
        if base_files:
            self._seed_base(workdir / "_base", base_files)
        # The generation-time self-heal rung reuses the deploy repair prompt; the
        # container appends the live native build/test logs before re-invoking
        # claude. Written to /out (NOT into the project) so it is never tarred out.
        try:
            (workdir / "repair_prompt.txt").write_text(
                self._load_prompt("backend_project_repair_prompt.txt"), encoding="utf-8"
            )
        except Exception:  # noqa: BLE001 — repair rung degrades to no-op if absent
            (workdir / "repair_prompt.txt").write_text("", encoding="utf-8")
        # BMAD reinforce prompt (second pass). Fail-soft: an empty file makes the
        # container skip the reinforce pass (`[ -s ]` is false), degrading to a
        # single generation — so a missing template never sinks the run. SKIPPED in
        # edit mode: a fresh-generation reinforce pass would rewrite the inherited
        # project from FR/NFR anchors, undoing the targeted续改.
        if edit_mode:
            (workdir / "reinforce_prompt.txt").write_text("", encoding="utf-8")
        else:
            try:
                (workdir / "reinforce_prompt.txt").write_text(
                    self._fill(self._load_prompt("backend_project_reinforce_prompt.txt"), **fill_vals),
                    encoding="utf-8",
                )
            except Exception:  # noqa: BLE001 — reinforce rung degrades to skip if absent
                (workdir / "reinforce_prompt.txt").write_text("", encoding="utf-8")
        # Anthropic credential injection: ANTHROPIC_AUTH_TOKEN (+ ANTHROPIC_BASE_URL)
        # for a gateway like zentao.panlaxy.io, else ANTHROPIC_API_KEY. Secrets are
        # passed by NAME (value pulled from `env` below) so they never land in argv.
        cred_flags, cred_env = anthropic_agent_credentials()

        user = container_user()
        cmd = [
            self.docker, "run", "--rm",
            "--user", user,
            *cred_flags,
            "-e", "OPENAI_API_KEY",
            "-e", f"BE_AGENT_TIMEOUT={self.gen_timeout}",
            "-e", f"BE_AGENT_INSTALL_TIMEOUT={self.install_timeout}",
            "-e", f"BE_AGENT_BUILD_TIMEOUT_GEN={self.build_timeout_gen}",
            "-e", f"BE_AGENT_TEST_TIMEOUT={self.test_timeout}",
            "-e", f"BE_AGENT_PACKAGE_TIMEOUT={self.package_timeout}",
            "-e", f"BE_AGENT_GEN_REPAIRS={self.gen_repairs}",
            "-e", f"BE_AGENT_REPAIR_TIMEOUT={self.repair_timeout}",
            "-e", f"BE_AGENT_REINFORCE={self.reinforce_enabled}",
            "-e", f"BE_AGENT_REINFORCE_TIMEOUT={self.reinforce_timeout}",
            "-v", f"{host_workdir(workdir)}:/out", self.image, "bash", "-c", _CONTAINER_SCRIPT,
        ]
        if user != "node":
            # When matching the host UID the image's /home/node is owned by uid
            # 1000 and is not writable. Use /tmp as HOME instead (always 1777).
            cmd += ["-e", "HOME=/tmp"]
        env = dict(os.environ, **cred_env)
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
        # Generation-time build ladder outcome: build_state is one of green /
        # green-repaired / install-failed / compile-failed / test-failed /
        # stack-unknown (manifest undetected → nothing built); degraded is that
        # state (empty when green). build_logs carries the per-phase tails for
        # diagnostics, and feeds the failure message + the publish meta.
        build_state = self._read_text(workdir / "build_state").strip() or "unknown"
        repaired_rounds = self._read_exit_code(workdir / "repaired_rounds") or 0
        degraded_reason = self._read_text(workdir / "degraded").strip() or None
        scaffold = self._read_text(workdir / "scaffold").strip() or "none"
        dockerfile_warn = self._read_text(workdir / "dockerfile_warn").strip()
        build_logs = {
            name: self._clip(text, 1500)
            for name in ("install", "compile", "test", "package")
            if (text := self._read_text(workdir / f"{name}.log")).strip()
        }
        # Whether a real test command was attempted (vs. an explicit skip): read the
        # DETERMINISTIC marker the test phase writes (1/0), NOT a log substring —
        # the log echoes the command string, which would self-pollute any heuristic.
        tests_ran = self._read_text(workdir / "tests_ran_marker").strip() == "1"
        # BMAD reinforce pass outcome: "skipped" (disabled/no template), "0" (ran
        # clean), or a non-zero claude exit. Diagnostic only.
        reinforce_state = self._read_text(workdir / "reinforce_exit").strip() or None
        # Back-compat alias: callers that read the old ``validate_log`` key still
        # get a useful combined build-log tail.
        validate_log = "\n".join(f"[{k}]\n{v}" for k, v in build_logs.items())[:4000]
        files = self._collect(workdir / "project")

        summary, usage, cost, is_error = "", {}, 0.0, True
        if result_events:
            # Take the LAST result so the summary reflects the post-reinforce state
            # (first-gen + reinforce both stream a terminal result); cost/usage are
            # summed across every pass, so they already include the reinforce pass.
            summary = next(
                (r.get("result") for r in reversed(result_events) if isinstance(r.get("result"), str)), ""
            )
            cost = sum(float(r.get("total_cost_usd") or 0.0) for r in result_events)
            usage = self._merge_usage(result_events)
            is_error = bool(result_events[-1].get("is_error"))

        has_dockerfile = any(Path(k).name == "Dockerfile" for k in files)
        # success stays "agent produced source + a Dockerfile" (so a buildable-but-
        # not-yet-green project is still publishable + deployable, where a SECOND
        # ladder + the contract gate apply). A red native build after the self-heal
        # rounds is surfaced as ``degraded`` instead of a hard failure (see the
        # workflow's failure policy).
        success = (docker_exit == 0) and bool(files) and has_dockerfile
        degraded = success and build_state not in ("green", "green-repaired")
        dockerfile_source = files.get("Dockerfile")
        return {
            "success": success,
            "error": None if success else self._format_failure(
                docker_exit=docker_exit, claude_exit=claude_exit, is_error=is_error,
                files=files, has_dockerfile=has_dockerfile, summary=summary,
                stderr=stderr, non_json_stdout=non_json_stdout, build_logs=build_logs,
            ),
            "files": files,
            "stack": stack,
            "dockerfile_origin": dockerfile_origin,  # 'agent' | 'synthesized'
            "dockerfile_source": (
                dockerfile_source.decode("utf-8", "replace")
                if isinstance(dockerfile_source, (bytes, bytearray)) else dockerfile_source
            ),
            "validate_log": validate_log,
            "build_state": build_state,  # green | green-repaired | *-failed | unknown
            "degraded": degraded,
            "degraded_reason": degraded_reason if degraded else None,
            "build_repaired_rounds": repaired_rounds,
            "build_logs": build_logs,
            "tests_ran": tests_ran,
            "scaffold": scaffold,  # make | ci | none
            "dockerfile_warn": dockerfile_warn or None,
            "reinforce_state": reinforce_state,  # 'skipped' | '0' | non-zero exit
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
        prompt = self._load_prompt("backend_project_repair_prompt.txt") + "\n\n" + (build_log or "")[-8000:]
        return self._run_repair_agent(workdir, prompt, on_log=on_log, is_cancelled=is_cancelled)

    def repair_contract(
        self,
        *,
        workdir: str,
        failures_digest: str,
        on_log: Optional[Callable[[str], None]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> dict:
        """AI self-heal a RUNNING backend whose deploy-time frontend↔backend
        integration test found interface defects (5xx / missing response fields /
        non-JSON). Same in-place mechanism as ``repair_build`` (mounts the staged
        source at ``/out``, pipes the prompt over stdin, claude edits files), but
        the prompt is the contract-conformance repair template + the failing test
        cases / container logs / contract digest. The deploy step then re-builds the
        image, restarts the container and re-runs the SAME test plan. Returns
        ``{ran, summary, cost_usd}``."""
        prompt = (
            self._load_prompt("backend_project_contract_repair_prompt.txt")
            + "\n\n" + (failures_digest or "")[-12000:]
        )
        return self._run_repair_agent(workdir, prompt, on_log=on_log, is_cancelled=is_cancelled)

    def repair_5xx(
        self,
        *,
        workdir: str,
        failures_digest: str,
        on_log: Optional[Callable[[str], None]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> dict:
        """Comprehensive deploy-time backend self-heal driven by the OpenAI Codex CLI.

        ONE Codex pass fixes EVERYTHING observed on the running backend this round —
        database / data-layer, runtime (5xx) crashes and interface defects — editing
        the staged source mounted at ``/out`` in place; the deploy step then rebuilds
        + swaps + re-checks (health + smoke + itest). Same in-place mechanism as
        ``repair_contract`` but uses Codex (`codex exec`, the be-agent image pre-bakes
        it) and a comprehensive aggregated brief (data-layer state + smoke 5xx + itest
        failures + container stack traces + contract). Returns ``{ran, summary,
        cost_usd}``; ``ran`` is False when ``OPENAI_API_KEY`` is unset (the caller then
        skips the repair rung and proceeds best-effort)."""
        # Keep the HEAD of the digest (data-layer state + frontend real-source
        # reference + smoke 5xx + itest cases + stack-trace logs are front-loaded by
        # _build_comprehensive_digest; the contract reference trails). A tail-slice
        # here would drop exactly the data-layer + FE-source + smoke sections the
        # comprehensive brief exists to feed. 32000 leaves comfortable headroom over
        # the worst-case default brief (~24.6k) so the trailing itest failures + logs
        # + contract are NOT truncated even when APP_ITEST_MAX_TESTS is raised above
        # its default — the prompt rides a temp file in-container (no argv limit), so a
        # larger cap only trades a little cost/latency for completeness.
        prompt = (
            self._load_prompt("backend_project_5xx_repair_prompt.txt")
            + "\n\n" + (failures_digest or "")[:32000]
        )
        return self._run_codex_repair_agent(workdir, prompt, on_log=on_log, is_cancelled=is_cancelled)

    def _run_repair_agent(
        self,
        workdir: str,
        prompt: str,
        *,
        on_log: Optional[Callable[[str], None]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> dict:
        """Run the in-place be-agent repair container with a ready-built prompt.

        Shared by ``repair_build`` (build/startup failures) and ``repair_contract``
        (interface defects). The prompt is piped over stdin so it never pollutes the
        docker build context. Returns ``{ran, summary, cost_usd}``; ``ran`` is False
        when no provider is configured (the caller then skips the repair rung).
        """
        if not self.is_configured():
            return {"ran": False, "summary": "", "cost_usd": 0.0, "error": "ANTHROPIC_API_KEY not configured"}
        wd = Path(workdir)
        if not wd.is_dir():
            return {"ran": False, "summary": "", "cost_usd": 0.0, "error": "workdir missing"}
        # The container runs as the non-root ``node`` uid; make the staged tree
        # writable so claude can edit the existing (backend-written) files.
        self._make_writable(wd)
        # Same Anthropic credential injection as the generation run (gateway-aware).
        cred_flags, cred_env = anthropic_agent_credentials()
        user = container_user()
        cmd = [
            self.docker, "run", "--rm", "-i",
            "--user", user,
            *cred_flags, "-e", "OPENAI_API_KEY",
            "-v", f"{host_workdir(wd)}:/out", self.image, "bash", "-c", _REPAIR_SCRIPT,
        ]
        if user != "node":
            cmd += ["-e", "HOME=/tmp"]
        env = dict(os.environ, **cred_env)
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

    def _run_codex_repair_agent(
        self,
        workdir: str,
        prompt: str,
        *,
        on_log: Optional[Callable[[str], None]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
    ) -> dict:
        """Run the in-place be-agent COMPREHENSIVE repair container driven by the
        OpenAI Codex CLI (``codex exec``). The aggregated brief is piped over the
        container's stdin so it never lands in /out (no build-context / promoted-source
        pollution); codex edits the staged source in place and the deploy step then
        rebuilds + swaps + re-checks. Codex streams PLAIN progress (not Claude's JSON
        events), so forward a throttled heartbeat to ``on_log`` and keep the tail as
        the summary. Returns ``{ran, summary, cost_usd}``; ``ran`` is False when
        ``OPENAI_API_KEY`` is unset (caller proceeds best-effort)."""
        if not self.is_codex_configured():
            return {"ran": False, "summary": "", "cost_usd": 0.0, "error": "OPENAI_API_KEY not configured"}
        wd = Path(workdir)
        if not wd.is_dir():
            return {"ran": False, "summary": "", "cost_usd": 0.0, "error": "workdir missing"}
        # The container runs as the non-root ``node`` uid; make the staged tree
        # writable so codex can edit the existing (backend-written) files.
        self._make_writable(wd)
        user = container_user()
        cmd = [
            self.docker, "run", "--rm", "-i",
            "--user", user,
            "-e", "OPENAI_API_KEY",
            "-e", f"BE_CODEX_REPAIR_TIMEOUT={self.codex_repair_timeout}",
            "-e", f"BE_CODEX_REPAIR_FLAGS={self.codex_repair_flags}",
            "-v", f"{host_workdir(wd)}:/out", self.image, "bash", "-c", _CODEX_REPAIR_SCRIPT,
        ]
        if user != "node":
            cmd += ["-e", "HOME=/tmp"]
        env = dict(os.environ, OPENAI_API_KEY=os.getenv("OPENAI_API_KEY") or "")

        tail: list[str] = []
        seen = 0
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
                tail.append(line)
                tail = tail[-60:]
                seen += 1
                # Codex is verbose — throttle the deploy timeline to a heartbeat.
                if on_log and seen % 25 == 1:
                    try:
                        on_log(f"Codex 修复中:{line[:160]}")
                    except Exception:  # noqa: BLE001
                        pass
                if is_cancelled and is_cancelled():
                    proc.kill()
                    return {"ran": True, "summary": "cancelled", "cost_usd": 0.0}
            # Host backstop slightly above the in-container `timeout`; None when the
            # in-container cap is disabled (0 = run to completion, mirrors gen).
            proc.wait(timeout=self.codex_repair_timeout + 60 if self.codex_repair_timeout > 0 else None)
        except subprocess.TimeoutExpired:
            proc.kill()
            return {"ran": True, "summary": "codex repair timed out", "cost_usd": 0.0}
        finally:
            if proc.stderr:
                err = proc.stderr.read()
                if err:
                    logger.info("be-agent codex repair stderr: %s", err[:1500])

        summary = " ".join(tail[-3:])[:300] if tail else ""
        if on_log and summary:
            try:
                on_log(f"Codex 修复完成:{summary[:160]}")
            except Exception:  # noqa: BLE001
                pass
        return {"ran": True, "summary": summary, "cost_usd": 0.0}

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
            "build_state": "unknown", "degraded": False, "degraded_reason": None,
            "build_repaired_rounds": 0, "build_logs": {}, "tests_ran": False,
            "scaffold": "none", "dockerfile_warn": None, "reinforce_state": None,
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
        summary, stderr, non_json_stdout, build_logs,
    ) -> str:
        reasons: list[str] = []
        mount_hint = mount_failure_hint(stderr)
        if mount_hint:
            reasons.append(mount_hint)
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
        for name, log in (build_logs or {}).items():
            if log:
                details.append(f"{name}: {self._clip(log)}")
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
