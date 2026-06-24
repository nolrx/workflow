# Sandbox image for the `code_backend_project_generation` workflow AND the
# deploy-time build self-heal round.
#
# Build:
#   docker build -t be-agent:latest backend/docker/be-agent
#   # (or, with the rest of the agent images:  docker compose --profile setup build)
# Then point the service at it (defaults to be-agent:latest, falling back to
# fe-agent:latest):
#   export BE_AGENT_IMAGE=be-agent:latest
#
# The workflow (backend/services/code/backend_project_service.py) starts a
# throwaway `--rm` container PER run, runs `claude` headless inside it as the
# non-root `node` user to WRITE a complete polyglot backend project (whatever the
# development flow chose) that implements the shared OpenAPI contract and ships
# its OWN Dockerfile, then copies the source to a host-mounted /out.
#
# Why this image now carries FULL per-language build toolchains (JDK+Maven, Go,
# Python, Node) — it used to be "write-only": the deploy step `docker build`s the
# project's own Dockerfile, so generation never needed a runtime. But the
# DEPLOY-TIME AI repair (backend_project_service.repair_build) also runs claude
# INSIDE this container after a failed `docker build`; without toolchains it could
# only edit blind (it could not actually compile what it changed), so a single
# Java/Go/Python compile-error cascade defeated the one repair round. With the
# toolchains present, the repair agent compiles/builds the staged project for
# REAL (mvn / go build / pip / npm) and iterates until green before the host
# re-runs `docker build`. Polyglot-by-design: no language requires manual fixing.
#
# The GENERATION step now uses the same toolchains: after claude writes the
# project, the container runs a REAL native build ladder (install -> compile ->
# test -> package-verify, with bounded AI self-heal) so "can it be packaged" is
# verified at generation, not deferred to deploy. There is still NO docker.sock
# here — the actual image packaging (docker build) only happens at deploy time on
# the platform; generation proves native buildability + tests only. `make` is
# installed explicitly because the ladders prefer the generated Makefile targets.
#
# No app code or secrets are baked in; ANTHROPIC_API_KEY / OPENAI_API_KEY are
# injected at run time via `-e`. CLIs: claude (@anthropic-ai/claude-code) drives
# generation + the build self-heal rungs; codex (@openai/codex) drives the
# DEPLOY-TIME comprehensive repair (database + runtime 5xx + interface, one pass
# per round — see deploy_service / backend_project_service.repair_5xx).
FROM node:22

# --- OS deps + Java (Temurin 21 via Adoptium) -------------------------------
# node:22 is Debian bookworm based; Adoptium ships a bookworm apt channel.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv git ca-certificates curl gnupg \
        make build-essential; \
    install -m 0755 -d /etc/apt/keyrings; \
    curl -fsSL https://packages.adoptium.net/artifactory/api/gpg/key/public \
        | gpg --dearmor -o /etc/apt/keyrings/adoptium.gpg; \
    echo "deb [signed-by=/etc/apt/keyrings/adoptium.gpg] https://packages.adoptium.net/artifactory/deb bookworm main" \
        > /etc/apt/sources.list.d/adoptium.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends temurin-21-jdk; \
    rm -rf /var/lib/apt/lists/*

# --- Maven (binary tarball; arch-independent JVM tool) ----------------------
# Pull from archive.apache.org (permanent host for ALL released versions);
# dlcdn.apache.org only keeps the current release and 404s on pinned older ones.
ENV MAVEN_VERSION=3.9.9
RUN set -eux; \
    curl -fsSL "https://archive.apache.org/dist/maven/maven-3/${MAVEN_VERSION}/binaries/apache-maven-${MAVEN_VERSION}-bin.tar.gz" -o /tmp/maven.tar.gz; \
    tar -xzf /tmp/maven.tar.gz -C /opt; \
    rm /tmp/maven.tar.gz; \
    ln -s "/opt/apache-maven-${MAVEN_VERSION}/bin/mvn" /usr/local/bin/mvn

# --- Go (official tarball; arch-aware) --------------------------------------
ENV GO_VERSION=1.22.7
RUN set -eux; \
    ARCH="$(dpkg --print-architecture)"; \
    case "$ARCH" in amd64) GOARCH=amd64 ;; arm64) GOARCH=arm64 ;; *) GOARCH=amd64 ;; esac; \
    curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-${GOARCH}.tar.gz" -o /tmp/go.tar.gz; \
    tar -C /usr/local -xzf /tmp/go.tar.gz; \
    rm /tmp/go.tar.gz; \
    ln -s /usr/local/go/bin/go /usr/local/bin/go; \
    ln -s /usr/local/go/bin/gofmt /usr/local/bin/gofmt
ENV PATH="/usr/local/go/bin:${PATH}"

# Pre-bake both agent CLIs so each run starts fast (no per-run global install).
# The smoke also asserts `make` (the generation/repair build ladders prefer the
# generated project's Makefile targets — make MUST be explicit, not relied on as
# an implicit base-image package, or a future base bump silently breaks it).
RUN npm install -g @anthropic-ai/claude-code @openai/codex \
    && claude --version \
    && codex --version \
    && mvn -version \
    && go version \
    && java -version \
    && make --version

# Default to the non-root user; the workflow also passes --user node explicitly.
# A writable HOME is required for Claude's ~/.claude, Codex's ~/.codex, Maven's
# ~/.m2, Go's ~/go + ~/.cache/go-build, and pip's ~/.cache.
USER node
ENV HOME=/home/node
WORKDIR /tmp/work
