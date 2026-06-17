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
FROM node:22

# Pre-bake the agent CLI so each run starts fast (no per-run global install).
RUN npm install -g @anthropic-ai/claude-code && claude --version

# Default to the non-root user; the workflow also passes --user node explicitly.
USER node
WORKDIR /tmp/work
