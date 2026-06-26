"""
Central credit pricing table.

Single source of truth for how many credits each AI-consuming operation costs.
Every value is overridable via an environment variable so ops can tune pricing
without a redeploy. Keep this module dependency-free (no Flask / DB imports) so
it can be imported from routes, services and background tasks alike.

NOTE: this lives under ``backend/services`` (not ``backend/config``) on purpose —
``backend/config.py`` is an existing module loaded as ``backend.config.<Name>Config``
in ``app.py``; turning ``config`` into a package would shadow it and break startup.
"""
import os


def _credits(env_var: str, default: int) -> int:
    """Read a non-negative credit cost from the environment, falling back to default."""
    try:
        value = int(os.getenv(env_var, default))
    except (TypeError, ValueError):
        return default
    return max(0, value)


# --- Code (agent) domain ------------------------------------------------------
# NOTE: Code-domain pricing is currently disabled (defaults 0) — the active
# version runs Code workflows free of charge / without a credit gate. The
# central charge()/refund_credits()/deduct-skip logic all treat 0 as a no-op,
# so set the PRICE_CODE_* env vars to re-enable metering without code changes.
CODE_FULL_GENERATION = _credits("PRICE_CODE_FULL", 0)       # base workflow cost
CODE_CONTEXT_VERIFY = _credits("PRICE_CODE_CONTEXT_VERIFY", 0)  # per AI context-consistency gate

# Up-front reservation for the full code workflow (requirements -> flow ->
# document split -> style -> previews -> publish). Context-verify gates are
# charged per-call as they fire (not folded in), so runs that never trigger a
# gate — e.g. no AI provider configured — are not over-charged.
CODE_FULL_GENERATION_TOTAL = CODE_FULL_GENERATION

# Agentic frontend PROJECT generation: an autonomous coding CLI in a sandboxed
# container produces a real multi-file React/Vite/TS project it builds itself
# (~$1 / run in PoC measurements). Much heavier than the single-file HTML path
# above, so priced higher; metered against the CLI's reported token usage.
CODE_FRONTEND_PROJECT_GENERATION = _credits("PRICE_CODE_FRONTEND_PROJECT", 0)

# Inline section (partial) revision of a confirmed Code document — the user
# selects a span of one stage product (requirements / flow / style / a single
# split document) and asks the model to rewrite just that span while keeping the
# rest byte-identical. A single synchronous text generation; priced 0 like the
# rest of the Code domain but routed through charge()/refund_credits() so it can
# be metered via PRICE_CODE_SECTION_REVISE without code changes.
CODE_SECTION_REVISION = _credits("PRICE_CODE_SECTION_REVISE", 0)

# Figma export of a generated HTML app: one model call that converts the HTML
# into a Design IR (node tree) for the companion plugin to rebuild as layers.
# The preview-image export path is deterministic (no model) and is never charged.
CODE_FIGMA_EXPORT = _credits("PRICE_CODE_FIGMA_EXPORT", 0)

# Figma SLICE export: an OpenAI Codex CLI runs headless in a sandboxed container
# to analyse a preview thumbnail into an EDITABLE Design IR (text/vector/sliced
# image), so Figma gets adjustable layers instead of one flat image. An
# agent-execution lane like the frontend PROJECT path; reserved up-front and
# auto-refunded only when the run produces nothing. Defaults 0 like the rest of
# the Code domain — set PRICE_CODE_FIGMA_SLICE to meter. NOTE for future metering:
# a fallback (degraded == single flat image) delivers the legacy preview_image
# result, so it should be discounted/refunded rather than charged in full.
CODE_FIGMA_SLICE = _credits("PRICE_CODE_FIGMA_SLICE", 0)
CODE_FIGMA_SLICE_TOTAL = CODE_FIGMA_SLICE

# Remix canvas (n8n-style): an up-front reservation per canvas run, plus a
# per-agent-node charge folded into the run's reported extra credits. Both default
# 0 (free) like the rest of the Code domain; set PRICE_CODE_CANVAS_* to meter.
CODE_CANVAS_RUN = _credits("PRICE_CODE_CANVAS_RUN", 0)
CODE_CANVAS_NODE = _credits("PRICE_CODE_CANVAS_NODE", 0)

# --- Full-stack generation (frontend + backend + middleware, concurrent) -------
# The fullstack orchestration first synthesizes ONE shared OpenAPI contract (a
# single text-model call), then fans out three concurrent agent runs that
# implement / consume it, and finally an atomic deploy run brings the generated
# app up behind a reverse proxy. Each piece is metered independently; all default
# 0 (free) like the rest of the Code domain — set the PRICE_* envs to enable.
CODE_CONTRACT_SYNTHESIS = _credits("PRICE_CODE_CONTRACT_SYNTHESIS", 0)  # shared API contract
# Agentic backend PROJECT generation: a coding CLI in a sandboxed container
# produces a polyglot multi-file backend (with its own Dockerfile) implementing
# the shared contract. Same agent-execution lane as the frontend project path.
CODE_BACKEND_PROJECT_GENERATION = _credits("PRICE_CODE_BACKEND_PROJECT", 0)
# Middleware provisioning: derives the data/cache layer from the manifest and
# generates the schema / migrations / seed (applied at deploy time).
CODE_MIDDLEWARE_PROVISIONING = _credits("PRICE_CODE_MIDDLEWARE", 0)
# Atomic deploy run: builds the generated backend image, provisions the
# per-project middleware namespace, starts the long-lived container, wires the
# reverse proxy and health-checks — with rollback on any failure.
CODE_FULLSTACK_DEPLOY = _credits("PRICE_CODE_FULLSTACK_DEPLOY", 0)
# Deploy-time frontend↔backend integration test: one AI call reads the shared
# contract + the generated frontend's API-calling code and distills a targeted
# test plan, which the deploy then EXECUTES against the live backend container to
# gate on real interface defects (5xx / response-shape mismatch). Charged per-call
# as the gate fires (folded into the deploy run like CODE_CONTEXT_VERIFY, not the
# up-front reservation) so a deploy with no provider configured isn't over-charged.
CODE_FULLSTACK_INTEGRATION_TEST = _credits("PRICE_CODE_FULLSTACK_ITEST", 0)

# --- Secondary development (二次开发 / 应用空间 iteration) -----------------------
# A lightweight planning run that reads the deployed app's requirements / flow /
# shared contract + the user's change ask and emits an impact analysis + a
# user-confirmable execution plan (one or two text-model calls; degrades to a
# deterministic plan when no provider is configured). The actual code regen reuses
# the existing frontend/backend/middleware lane costs + the deploy cost, so only
# the analysis itself is metered here. Defaults 0 (free) like the rest of Code.
CODE_APP_ITERATION_ANALYSIS = _credits("PRICE_CODE_ITERATION_ANALYSIS", 0)


# Operation label -> (resource_type, unit_cost). Used when writing CreditTransaction
# records so the audit log carries a stable operation/resource vocabulary.
OPERATION = {
    "agent_run": ("agent_run", CODE_FULL_GENERATION_TOTAL),
    "code_context_verify": ("agent_run", CODE_CONTEXT_VERIFY),
    "code_frontend_project": ("agent_run", CODE_FRONTEND_PROJECT_GENERATION),
    "code_section_revise": ("code_project", CODE_SECTION_REVISION),
    "code_figma_export": ("code_project", CODE_FIGMA_EXPORT),
    "code_figma_slice": ("agent_run", CODE_FIGMA_SLICE_TOTAL),
    "code_canvas_run": ("agent_run", CODE_CANVAS_RUN),
    "code_canvas_node": ("agent_run", CODE_CANVAS_NODE),
    "code_contract_synthesis": ("code_project", CODE_CONTRACT_SYNTHESIS),
    "code_backend_project": ("agent_run", CODE_BACKEND_PROJECT_GENERATION),
    "code_middleware": ("agent_run", CODE_MIDDLEWARE_PROVISIONING),
    "code_fullstack_deploy": ("agent_run", CODE_FULLSTACK_DEPLOY),
    "code_fullstack_itest": ("agent_run", CODE_FULLSTACK_INTEGRATION_TEST),
    "code_app_iteration_analysis": ("agent_run", CODE_APP_ITERATION_ANALYSIS),
}
