# Sandbox image for the `code_backend_project_generation` workflow.
#
# Build:
#   docker build -t be-agent:latest backend/docker/be-agent
# Then point the service at it (defaults to be-agent:latest, falling back to
# fe-agent:latest):
#   export BE_AGENT_IMAGE=be-agent:latest
#
# The workflow (backend/services/code/backend_project_service.py) starts a
# throwaway `--rm` container PER run, runs `claude` headless inside it as the
# non-root `node` user to WRITE a complete polyglot backend project (whatever the
# development flow chose) that implements the shared OpenAPI contract and ships
# its OWN Dockerfile, then copies the source to a host-mounted /out. The project
# is NOT built/run here — the deploy step `docker build`s the project's Dockerfile
# (so this image needs no per-language runtime, only enough to WRITE + lightly
# syntax-check the code). No app code or secrets are baked in; ANTHROPIC_API_KEY
# is injected at run time via `-e`.
#
# CLIs: claude (@anthropic-ai/claude-code) is the primary engine; codex
# (@openai/codex) is available for parity with fe-agent. python3 is added so the
# fail-soft syntax-check ladder can `py_compile` python backends (node --check
# covers JS; go/java checks degrade gracefully when their toolchains are absent —
# the real build happens at deploy time from the project's own Dockerfile).
FROM node:22

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 git \
    && rm -rf /var/lib/apt/lists/*

# Pre-bake both agent CLIs so each run starts fast (no per-run global install).
RUN npm install -g @anthropic-ai/claude-code @openai/codex \
    && claude --version \
    && codex --version

# Default to the non-root user; the workflow also passes --user node explicitly.
# A writable HOME is required for Claude's ~/.claude and Codex's ~/.codex.
USER node
ENV HOME=/home/node
WORKDIR /tmp/work
