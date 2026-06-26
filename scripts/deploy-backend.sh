#!/usr/bin/env bash
# Graceful single-machine redeploy of the platform backend.
#
#   drain (stop accepting new runs)  ->  build the new image  ->  recreate the
#   backend container  ->  wait for /health/ready  ->  done.
#
# In-flight runs are interrupted by the container swap and RESUMED by the new
# process from their persisted phase (reconcile_orphaned_runs); SSE clients
# auto-reconnect. This trades a few seconds of API blip for not losing in-flight
# work — true zero-downtime needs the worker-decoupling step (see
# docs/platform-deploy.md).
#
# Env knobs:
#   DEPLOY_CONTROL_TOKEN  shared token the backend checks (X-Deploy-Token). When
#                         unset, draining is skipped (runs still resume, but new
#                         work isn't refused during the swap window).
#   DEPLOY_BASE_URL       backend base url for control calls (default :5001).
#   DEPLOY_DRAIN_GRACE    seconds to let short in-flight runs settle (default 20).
#   DEPLOY_READY_TIMEOUT  seconds to wait for the new instance (default 120).
#   COMPOSE               compose binary (default "docker compose").
set -euo pipefail

COMPOSE="${COMPOSE:-docker compose}"
BASE_URL="${DEPLOY_BASE_URL:-http://localhost:5001}"
TOKEN="${DEPLOY_CONTROL_TOKEN:-}"
GRACE="${DEPLOY_DRAIN_GRACE:-20}"
READY_TIMEOUT="${DEPLOY_READY_TIMEOUT:-120}"

log() { printf '\033[36m[deploy]\033[0m %s\n' "$*"; }

# 1. Drain the live instance so it stops accepting new runs before the swap.
if [ -n "$TOKEN" ]; then
  log "draining current instance (no new runs accepted)…"
  if curl -fsS -X POST -H "X-Deploy-Token: $TOKEN" \
       "$BASE_URL/api/admin/lifecycle/drain" >/dev/null 2>&1; then
    log "drain ON — waiting ${GRACE}s for short in-flight runs to settle"
    sleep "$GRACE"
  else
    log "drain call failed (instance down or token mismatch) — continuing"
  fi
else
  log "DEPLOY_CONTROL_TOKEN unset — skipping drain (in-flight runs still resume)"
fi

# 2. Build the new image BEFORE recreating, to keep the swap window short.
log "building backend image…"
$COMPOSE build backend

# 3. Recreate ONLY the backend container (middleware/services untouched). On boot
#    the new process resumes any run the old one left in-flight.
log "recreating backend container…"
$COMPOSE up -d --no-deps backend

# 4. Wait for the fresh process to report ready (a new process never starts
#    draining, so /health/ready flips back to 200 once it's up).
log "waiting for /health/ready (timeout ${READY_TIMEOUT}s)…"
deadline=$(( $(date +%s) + READY_TIMEOUT ))
until curl -fsS "$BASE_URL/health/ready" >/dev/null 2>&1; do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    log "ERROR: backend not ready within ${READY_TIMEOUT}s — check 'make logs S=backend'"
    log "rollback: redeploy the previous image tag, e.g. '$COMPOSE up -d --no-deps backend'"
    exit 1
  fi
  sleep 2
done

# NOTE: additive schema changes (new model columns) are self-healed at BOOT by
# backend/services/schema_guard.py::ensure_model_columns — every boot, before
# serving — so a redeploy needs NO migration step for them. Alembic is reserved
# for non-additive migrations (rename/drop/type/data) and is run MANUALLY when
# needed: `make migrate-prod` (inside the container) — deliberately not auto-run
# here to avoid a redundant alembic_version side effect on the platform DB.

# 5. Reload nginx so it re-resolves the backend service name — a recreated
#    container can get a NEW IP, and the upstream is resolved at config-load time,
#    so without a reload the frontend would 502 against the old address.
#    Best-effort: a bare-metal / non-compose nginx isn't ours to reload here.
if $COMPOSE exec -T frontend nginx -s reload >/dev/null 2>&1; then
  log "reloaded frontend nginx (picked up new backend address)"
else
  log "note: could not reload frontend nginx (not running under compose?) — reload it if users hit 502"
fi

log "backend ready — in-flight runs resume automatically. Done."
