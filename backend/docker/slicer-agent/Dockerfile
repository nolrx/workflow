# Sandbox image for the `code_figma_slice_generation` workflow.
#
# Build:
#   docker build -t slicer-agent:latest backend/docker/slicer-agent
# Then point the service at it (defaults to slicer-agent:latest):
#   export SLICER_AGENT_IMAGE=slicer-agent:latest
#
# The workflow (backend/services/code/figma_slice_service.py) starts a throwaway
# `--rm` container PER run, runs `codex` (OpenAI Codex CLI) headless inside it as
# the non-root `node` user to analyse a preview thumbnail into a Design IR, then
# a deterministic Python (Pillow) script crops the IMAGE regions out of the
# source PNG. Only `ir.json` + the cropped slices are copied to a host-mounted
# /out; everything else lives in the container fs and dies with `--rm`. No app
# code or secrets are baked in; OPENAI_API_KEY is injected at run time via `-e`.
FROM node:22-bookworm

# Pillow for the deterministic crop step (mirrors the backend's PIL usage).
# git: the Codex CLI expects to run inside a working tree; the container script
# `git init`s the scratch dir before invoking it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pil git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Pre-bake the agent CLI so each run starts fast (no per-run global install).
RUN npm install -g @openai/codex && codex --version

# Default to the non-root user; the workflow also passes --user node explicitly.
# A writable HOME is required for Codex's ~/.codex config/session dir.
USER node
ENV HOME=/home/node
WORKDIR /tmp/work
