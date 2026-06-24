FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI (client only — NOT the daemon) for the Docker-out-of-Docker lane.
# The code_frontend_project_generation workflow shells out to `docker run` to
# spawn a throwaway fe-agent container; with the host /var/run/docker.sock
# bind-mounted (see docker-compose.yml) this client talks to the HOST daemon and
# creates sibling containers. We extract just the `docker` binary from the static
# release tarball — the bundled dockerd/containerd are intentionally discarded.
ARG DOCKER_CLI_VERSION=27.5.1
RUN set -eux; \
    case "$(uname -m)" in \
      x86_64)  dl=x86_64 ;; \
      aarch64) dl=aarch64 ;; \
      *) echo "unsupported arch: $(uname -m)" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${dl}/docker-${DOCKER_CLI_VERSION}.tgz" -o /tmp/docker.tgz; \
    tar -xzf /tmp/docker.tgz -C /tmp docker/docker; \
    mv /tmp/docker/docker /usr/local/bin/docker; \
    rm -rf /tmp/docker /tmp/docker.tgz; \
    docker --version

COPY pyproject.toml ./
COPY backend/__init__.py ./backend/__init__.py
RUN python -m pip install --upgrade pip \
    && python -m pip install -e .

COPY backend ./backend

RUN mkdir -p /app/backend/uploads /app/instance

EXPOSE 5001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5001/health', timeout=3).read()" || exit 1

# Production server. Server settings (single gthread worker, timeouts) and the
# graceful drain-on-SIGTERM hook live in backend/gunicorn.conf.py — a single
# worker is intentional (the Agent Swarm runtime / SSE bus / recorder are
# process-level singletons, so cross-request SSE replay only works in one process).
# `serve()` (not `create_app()`) is the factory: it is the one entrypoint that
# reconciles orphaned runs on boot. Plain create_app() stays side-effect-free so
# no script/test/ops one-liner can trigger a platform-wide resume storm.
CMD ["gunicorn", "backend.app:serve()", "-c", "backend/gunicorn.conf.py"]
