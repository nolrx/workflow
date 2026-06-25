#!/usr/bin/env bash
# Start backend + middleware in Docker, with agent images built.
# Frontend is NOT started; run `npm run dev:frontend` separately.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
echo "Repo root: $REPO_ROOT"

if ! docker info >/dev/null 2>&1; then
    echo "[ERROR] Docker is not running or not in PATH."
    echo "Please start Docker Desktop first."
    exit 1
fi

echo ""
echo "[1/3] Building agent images (fe-agent, be-agent, slicer-agent)..."
docker compose --profile setup build

echo ""
echo "[2/3] Building backend image..."
docker compose build backend

echo ""
echo "[3/3] Starting backend + middleware in Docker..."
docker compose up -d backend postgres redis mongo

echo ""
echo "[OK] Backend services started."
echo ""
echo "Useful commands:"
echo "  docker compose ps"
echo "  docker compose logs -f backend"
echo "  docker compose down"
echo ""
echo "Next step: start frontend locally with:"
echo "  npm run dev:frontend"
echo "Then open http://localhost:3000"
