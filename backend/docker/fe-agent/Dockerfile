# Sandbox image for the `code_frontend_project_generation` workflow.
#
# Build:
#   docker build -t fe-agent:latest backend/docker/fe-agent
# Then point the service at it (defaults to fe-agent:latest):
#   export FE_AGENT_IMAGE=fe-agent:latest
#
# The workflow (backend/services/code/frontend_project_service.py) starts a
# throwaway `--rm` container PER run, runs `claude` headless inside it as the
# non-root `node` user, and copies only source + built dist to a host-mounted
# /out. node_modules and the npm cache live solely in the container fs and die
# with the container. No app code or secrets are baked in; ANTHROPIC_API_KEY is
# injected at run time via `-e`.
#
# TWO agent CLIs co-exist in this image (mirrors the slicer-agent's Codex setup):
#   * claude  (@anthropic-ai/claude-code) — the primary engine: writes the
#     multi-file React/Vite/TS project and self-checks the build.
#   * codex   (@openai/codex)             — driven by Claude through the bundled
#     `image-assets` skill: when the UI needs REAL raster imagery (instead of
#     emoji / empty placeholders) Claude shells out to Codex, which generates the
#     pictures via the OpenAI image model and writes them into the project.
# Codex authenticates with OPENAI_API_KEY (also injected at run time via `-e`);
# both CLIs need a writable HOME for their config/session dirs.
FROM node:22

# Pre-bake both agent CLIs so each run starts fast (no per-run global install).
RUN npm install -g @anthropic-ai/claude-code @openai/codex \
    && claude --version \
    && codex --version

# Runtime browser-smoke (P1-C): a dedicated Playwright + Chromium install so the
# in-container runtime check (frontend_project_service.py -> runtimecheck.mjs) can
# load the built dist headless and capture console/page errors + a screenshot.
# Installed in an ISOLATED dir (resolved via createRequire) so it never collides
# with the generated project's own node_modules; browsers live in a world-readable
# path so the non-root `node` user can launch them. This layer is OPTIONAL — if it
# is removed the runtime check degrades fail-soft to ran:false (the build still
# ships); keep it to activate the verify->repair loop's runtime gate.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN mkdir -p /opt/runtime-smoke \
    && cd /opt/runtime-smoke \
    && npm init -y >/dev/null 2>&1 \
    && npm install playwright \
    && npx playwright install --with-deps chromium \
    && chmod -R a+rx /ms-playwright

# Default to the non-root user; the workflow also passes --user node explicitly.
# A writable HOME is required for Claude's ~/.claude (config + skills) and Codex's
# ~/.codex (auth.json + sessions); the container script writes both there.
USER node
ENV HOME=/home/node
WORKDIR /tmp/work
