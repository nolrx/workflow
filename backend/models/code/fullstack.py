"""
Full-stack generation models — the shared contract ledger and the per-project
deployment registry that tie the three concurrent Code workflows together.

``CodeProjectLedger`` is the single source of truth that lets the frontend,
backend and middleware runs agree on ONE API surface: it holds the synthesized
OpenAPI contract + middleware manifest (derived from the project's development
flow), keyed by project id. It is written ONCE by the orchestration endpoint
before the three runs start, and only READ by them — so the three concurrent
runs converge on the same contract without a cross-run write race. ``version`` is
an optimistic-lock guard for re-synthesis / future merge-back.

``CodeDeployment`` records the live deployment of a generated full-stack app: the
long-lived backend container, its allocated network identity, the per-project
middleware namespace (db / redis prefix), and the atomic-deploy status/health. It
is the routing table the backend's ``/app/<pid>/api`` reverse proxy resolves
against. Comments in English to match the Code/core convention.
"""
import json
import uuid
from datetime import datetime

from backend.extensions import db


class ContractStatus:
    """Lifecycle of the shared API contract synthesis."""

    PENDING = "pending"
    BUILDING = "building"
    READY = "ready"
    FAILED = "failed"


class DeploymentStatus:
    """Lifecycle of an atomic full-stack deployment."""

    PENDING = "pending"
    PROVISIONING = "provisioning"  # creating the per-project middleware namespace
    BUILDING = "building"  # docker build of the generated backend image
    STARTING = "starting"  # docker run + health check
    RUNNING = "running"  # backend container healthy + proxied
    FAILED = "failed"
    STOPPED = "stopped"
    ROLLED_BACK = "rolled_back"

    ACTIVE = {PENDING, PROVISIONING, BUILDING, STARTING, RUNNING}
    TERMINAL = {FAILED, STOPPED, ROLLED_BACK}


class CodeProjectLedger(db.Model):
    """Shared, project-keyed consensus: the synthesized API contract + manifest.

    One row per Code project. The orchestration endpoint synthesizes the OpenAPI
    contract + middleware manifest from the project's development flow and writes
    it here once; the frontend / backend / middleware runs each read it so they
    implement and consume the SAME endpoints. Never shown to end users.
    """

    __tablename__ = "code_project_ledgers"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(
        db.String(36),
        db.ForeignKey("code_projects.id"),
        nullable=False,
        unique=True,
        index=True,
    )
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    team_id = db.Column(db.String(36), db.ForeignKey("teams.id"), nullable=True, index=True)

    # The synthesized OpenAPI 3.x document (the cross-service contract).
    api_contract_raw = db.Column(db.Text, nullable=True)
    # The middleware manifest: which datastores/caches/queues the backend needs,
    # plus the schema/entities (derived from `## 数据设计`).
    middleware_manifest_raw = db.Column(db.Text, nullable=True)
    # The seed consensus ledger (ContextLedger.to_dict) the three runs branch from.
    shared_ledger_raw = db.Column(db.Text, nullable=True)

    contract_status = db.Column(
        db.String(20), nullable=False, default=ContractStatus.PENDING, index=True
    )
    # Optimistic-lock guard: bumped on every contract (re)synthesis.
    version = db.Column(db.Integer, nullable=False, default=0)
    error_message = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # ---- JSON helpers --------------------------------------------------------
    @staticmethod
    def _load(raw, default):
        if not raw:
            return default
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return default

    def get_api_contract(self) -> dict:
        return self._load(self.api_contract_raw, {})

    def set_api_contract(self, data: dict | None) -> None:
        self.api_contract_raw = json.dumps(data or {}, ensure_ascii=False)

    def get_middleware_manifest(self) -> dict:
        return self._load(self.middleware_manifest_raw, {})

    def set_middleware_manifest(self, data: dict | None) -> None:
        self.middleware_manifest_raw = json.dumps(data or {}, ensure_ascii=False)

    def get_shared_ledger(self) -> dict:
        return self._load(self.shared_ledger_raw, {})

    def set_shared_ledger(self, data: dict | None) -> None:
        self.shared_ledger_raw = json.dumps(data or {}, ensure_ascii=False)

    def to_dict(self, include_contract: bool = True) -> dict:
        data = {
            "id": self.id,
            "project_id": self.project_id,
            "contract_status": self.contract_status,
            "version": self.version,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "updated_at": self.updated_at.isoformat() + "Z" if self.updated_at else None,
        }
        if include_contract:
            data["api_contract"] = self.get_api_contract()
            data["middleware_manifest"] = self.get_middleware_manifest()
        return data


class CodeDeployment(db.Model):
    """The live deployment registry for a generated full-stack app.

    Keyed by project id (the latest deployment wins). The backend's
    ``/app/<pid>/api`` reverse proxy resolves ``container_name``/``internal_port``
    here; the deploy workflow drives the status machine and records rollback.
    """

    __tablename__ = "code_deployments"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(
        db.String(36),
        db.ForeignKey("code_projects.id"),
        nullable=False,
        unique=True,
        index=True,
    )
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    team_id = db.Column(db.String(36), db.ForeignKey("teams.id"), nullable=True, index=True)

    # Which runs produced the deployed artifacts (provenance / replay).
    frontend_run_id = db.Column(db.String(36), nullable=True)
    backend_run_id = db.Column(db.String(36), nullable=True)
    middleware_run_id = db.Column(db.String(36), nullable=True)
    deploy_run_id = db.Column(db.String(36), nullable=True)
    # The run whose ``site`` dir holds the frontend dist the preview should serve.
    # Normally None (preview serves the newest frontend-generation run's dist). When a
    # deploy BUILDS a fresh dist from a newer Dev Mode source snapshot, it writes that
    # dist under the deploy run's ``site`` dir and points this here — so the deployed
    # preview reflects the code tuned in Dev Mode, not the pre-dev generation build.
    # nullable → auto-added by schema_guard on existing tables (no Alembic).
    frontend_site_run_id = db.Column(db.String(36), nullable=True)

    # Live backend container identity (on the shared app network).
    container_name = db.Column(db.String(120), nullable=True)
    image_tag = db.Column(db.String(160), nullable=True)
    internal_port = db.Column(db.Integer, nullable=True)  # container-internal port (PORT env)
    api_base_path = db.Column(db.String(200), nullable=True)  # e.g. /app/<pid>/api

    # Per-project middleware namespace inside the shared infra.
    db_name = db.Column(db.String(120), nullable=True)
    redis_prefix = db.Column(db.String(120), nullable=True)

    status = db.Column(
        db.String(20), nullable=False, default=DeploymentStatus.PENDING, index=True
    )
    health = db.Column(db.String(20), nullable=True)  # healthy / unhealthy / unknown
    error_message = db.Column(db.Text, nullable=True)
    # Free-form JSON: build logs tail, health probe detail, rollback steps taken.
    detail_raw = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    deployed_at = db.Column(db.DateTime, nullable=True)

    def get_detail(self) -> dict:
        if not self.detail_raw:
            return {}
        try:
            return json.loads(self.detail_raw)
        except (json.JSONDecodeError, TypeError):
            return {}

    def set_detail(self, data: dict | None) -> None:
        self.detail_raw = json.dumps(data or {}, ensure_ascii=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "frontend_run_id": self.frontend_run_id,
            "backend_run_id": self.backend_run_id,
            "middleware_run_id": self.middleware_run_id,
            "deploy_run_id": self.deploy_run_id,
            "frontend_site_run_id": self.frontend_site_run_id,
            "container_name": self.container_name,
            "image_tag": self.image_tag,
            "internal_port": self.internal_port,
            "api_base_path": self.api_base_path,
            "db_name": self.db_name,
            "redis_prefix": self.redis_prefix,
            "status": self.status,
            "health": self.health,
            "error_message": self.error_message,
            "detail": self.get_detail(),
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "updated_at": self.updated_at.isoformat() + "Z" if self.updated_at else None,
            "deployed_at": self.deployed_at.isoformat() + "Z" if self.deployed_at else None,
        }


class IterationStatus:
    """Lifecycle of a secondary-development (二次开发) iteration.

    The happy path is draft → analyzing → awaiting_plan_approval → generating →
    staging_deploying → released. P0 deploys to the current deployment (there is
    one CodeDeployment per project); the staging_* states keep the door open for
    the P1 真 staging / promote split without a model change.
    """

    DRAFT = "draft"
    ANALYZING = "analyzing"  # the analysis run is judging impact scope
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"  # plan ready, waiting on the user
    GENERATING = "generating"  # the requested lane runs are in flight
    STAGING_DEPLOYING = "staging_deploying"  # the deploy run is bringing the new version up
    STAGING_READY = "staging_ready"  # deployed + healthy, awaiting explicit release (P1)
    RELEASE_PENDING = "release_pending"
    RELEASED = "released"  # the new version is the live deployment
    FAILED = "failed"
    CANCELLED = "cancelled"

    # Non-terminal: a reconcile pass may still advance these.
    ACTIVE = {
        DRAFT,
        ANALYZING,
        AWAITING_PLAN_APPROVAL,
        GENERATING,
        STAGING_DEPLOYING,
        STAGING_READY,
        RELEASE_PENDING,
    }
    TERMINAL = {RELEASED, FAILED, CANCELLED}


class IterationChangeType:
    """The user's declared intent for an iteration (drives the analysis prompt)."""

    BUG_FIX = "bug_fix"
    NEW_FEATURE = "new_feature"
    UI_CHANGE = "ui_change"
    BACKEND_LOGIC = "backend_logic"
    DATA_MODEL = "data_model"
    OTHER = "other"

    ALL = {BUG_FIX, NEW_FEATURE, UI_CHANGE, BACKEND_LOGIC, DATA_MODEL, OTHER}


class ImpactScope:
    """How far an iteration reaches — maps to the lanes that get (re)generated."""

    FRONTEND = "frontend"
    BACKEND = "backend"
    FRONTEND_BACKEND = "frontend_backend"
    BACKEND_MIDDLEWARE = "backend_middleware"
    FULLSTACK = "fullstack"

    ALL = {FRONTEND, BACKEND, FRONTEND_BACKEND, BACKEND_MIDDLEWARE, FULLSTACK}

    # impact_scope -> ordered generation lanes (canonical order frontend→backend→middleware).
    LANES = {
        FRONTEND: ["frontend"],
        BACKEND: ["backend"],
        FRONTEND_BACKEND: ["frontend", "backend"],
        BACKEND_MIDDLEWARE: ["backend", "middleware"],
        FULLSTACK: ["frontend", "backend", "middleware"],
    }

    @classmethod
    def lanes_for(cls, scope: str | None) -> list[str]:
        """The lanes a given impact scope (re)generates; defaults to backend-only."""
        return cls.LANES.get(scope or "", ["backend"])

    @classmethod
    def from_lanes(cls, lanes: list[str]) -> str:
        """Reverse map a recommended-lanes list back to the closest scope label."""
        s = set(lanes or [])
        if not s:
            return cls.BACKEND
        if s == {"frontend"}:
            return cls.FRONTEND
        if s == {"backend"}:
            return cls.BACKEND
        if s == {"frontend", "backend"}:
            return cls.FRONTEND_BACKEND
        if s == {"backend", "middleware"}:
            return cls.BACKEND_MIDDLEWARE
        # frontend+middleware, all three, or anything broader → fullstack.
        return cls.FULLSTACK


class CodeAppIteration(db.Model):
    """One secondary-development (二次开发) iteration of a deployed app.

    Every change to a live app is captured as an iteration so the online version
    stays read-only until an explicit deploy: the user states the change, an
    analysis run judges the impact scope + drafts an execution plan, the user
    confirms, then the requested generation lanes re-run and the app redeploys.
    Mirrors the multitenancy + JSON-in-Text conventions of the rest of the domain.
    """

    __tablename__ = "code_app_iterations"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(
        db.String(36), db.ForeignKey("code_projects.id"), nullable=False, index=True
    )
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    team_id = db.Column(db.String(36), db.ForeignKey("teams.id"), nullable=True, index=True)

    # The deployment this iteration branched from (provenance for rollback / audit).
    base_deployment_id = db.Column(db.String(36), nullable=True)

    instruction = db.Column(db.Text, nullable=False)  # the user's natural-language change ask
    change_type = db.Column(db.String(30), nullable=False, default=IterationChangeType.OTHER)
    # Resolved impact scope (analysis output, optionally user-overridden on confirm).
    impact_scope = db.Column(db.String(30), nullable=True)

    status = db.Column(
        db.String(30), nullable=False, default=IterationStatus.DRAFT, index=True
    )

    # Confirmation gates from the input form (section 六).
    allow_contract_change = db.Column(db.Boolean, nullable=False, default=False)
    allow_db_change = db.Column(db.Boolean, nullable=False, default=False)
    deploy_to_prod = db.Column(db.Boolean, nullable=False, default=True)

    # JSON-in-Text products of the analysis run.
    analysis_raw = db.Column(db.Text, nullable=True)  # the impact-analysis object
    plan_raw = db.Column(db.Text, nullable=True)  # the user-confirmable execution plan
    contract_diff_raw = db.Column(db.Text, nullable=True)  # optional API-contract diff (P1)

    # Run provenance — each lane / phase is a replayable AgentRun.
    analysis_run_id = db.Column(db.String(36), nullable=True)
    frontend_run_id = db.Column(db.String(36), nullable=True)
    backend_run_id = db.Column(db.String(36), nullable=True)
    middleware_run_id = db.Column(db.String(36), nullable=True)
    deploy_run_id = db.Column(db.String(36), nullable=True)

    error_message = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @staticmethod
    def _load(raw, default):
        if not raw:
            return default
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return default

    def get_analysis(self) -> dict:
        return self._load(self.analysis_raw, {})

    def set_analysis(self, data: dict | None) -> None:
        self.analysis_raw = json.dumps(data or {}, ensure_ascii=False)

    def get_plan(self) -> dict:
        return self._load(self.plan_raw, {})

    def set_plan(self, data: dict | None) -> None:
        self.plan_raw = json.dumps(data or {}, ensure_ascii=False)

    def get_contract_diff(self) -> dict:
        return self._load(self.contract_diff_raw, {})

    def set_contract_diff(self, data: dict | None) -> None:
        self.contract_diff_raw = json.dumps(data or {}, ensure_ascii=False)

    def lane_run_ids(self) -> dict:
        """The generation-lane run ids recorded on this iteration (skips empty)."""
        return {
            k: v
            for k, v in {
                "frontend": self.frontend_run_id,
                "backend": self.backend_run_id,
                "middleware": self.middleware_run_id,
            }.items()
            if v
        }

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "base_deployment_id": self.base_deployment_id,
            "instruction": self.instruction,
            "change_type": self.change_type,
            "impact_scope": self.impact_scope,
            "status": self.status,
            "allow_contract_change": self.allow_contract_change,
            "allow_db_change": self.allow_db_change,
            "deploy_to_prod": self.deploy_to_prod,
            "analysis": self.get_analysis(),
            "plan": self.get_plan(),
            "contract_diff": self.get_contract_diff(),
            "analysis_run_id": self.analysis_run_id,
            "frontend_run_id": self.frontend_run_id,
            "backend_run_id": self.backend_run_id,
            "middleware_run_id": self.middleware_run_id,
            "deploy_run_id": self.deploy_run_id,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "updated_at": self.updated_at.isoformat() + "Z" if self.updated_at else None,
        }


class DevSessionStatus:
    """Lifecycle of an interactive Dev Mode session.

    A DevSession owns ONE long-running dev container (``npm run dev`` + Vite HMR)
    and a persistent checklist; it lives OUTSIDE the batch run lifecycle (like a
    CodeDeployment) so it survives across turns and is only torn down on explicit
    deploy / stop / idle-reap. ``repairing`` marks a self-heal turn in flight.
    """

    STARTING = "starting"  # container being created + npm install + npm run dev
    RUNNING = "running"  # dev server up, ready to accept turns / preview
    REPAIRING = "repairing"  # crash detected, a self-heal turn is running
    STOPPED = "stopped"  # explicitly stopped (deploy transition / user / idle reap)
    FAILED = "failed"  # container failed to come up

    # Non-terminal: a reconcile pass may still advance / heal these.
    ACTIVE = {STARTING, RUNNING, REPAIRING}
    TERMINAL = {STOPPED, FAILED}


class DevTaskStatus:
    """Persistent task state machine for a Dev Mode session's backlog.

    The original four states (pending / in_progress / done / skipped) are the
    user-visible checklist; the sprint scheduler adds the full lifecycle:

        pending -> queued -> in_progress -> verifying -> done
                                 |               |-> pending   (retryable fail)
                                 |               |-> blocked   (retries exhausted / needs user)
                                 |-> failed                    (infra / run exception)
        any non-terminal -> cancelled                          (sprint cancelled)

    ``ready`` is intentionally NOT stored — it is derived:
    ``ready = status == pending and all depends_on are done``.
    """

    PENDING = "pending"
    QUEUED = "queued"  # claimed by the sprint scheduler; a turn run is being started
    IN_PROGRESS = "in_progress"  # the turn's agent is editing code
    VERIFYING = "verifying"  # edit finished; acceptance / regression checks running
    DONE = "done"
    BLOCKED = "blocked"  # exhausted retries / unmet deps / needs a user decision
    FAILED = "failed"  # infra-level failure (container crash / run exception)
    SKIPPED = "skipped"
    CANCELLED = "cancelled"

    ALL = {PENDING, QUEUED, IN_PROGRESS, VERIFYING, DONE, BLOCKED, FAILED, SKIPPED, CANCELLED}
    # What the user may set directly via PATCH (the pre-sprint vocabulary); the
    # scheduler-owned states are only ever written by the state machine helpers.
    USER_SETTABLE = {PENDING, IN_PROGRESS, DONE, SKIPPED}
    # Claimed by a sprint/turn — in flight right now.
    ACTIVE = {QUEUED, IN_PROGRESS, VERIFYING}
    TERMINAL = {DONE, BLOCKED, FAILED, SKIPPED, CANCELLED}
    # Counted as "delivered" for progress %; SKIPPED is neither done nor pending.
    DELIVERED = {DONE}
    # Terminal states that satisfy a sprint's completion (skipped is an explicit
    # user/scheduler decision, so it doesn't hold a sprint open).
    SETTLED_OK = {DONE, SKIPPED}


class DevTaskSource:
    """Where a checklist item came from (provenance for the progress board)."""

    LEDGER_SEED = "ledger_seed"  # derived from the context ledger's FR/NFR at start
    USER_ADDED = "user_added"  # the user typed a new feature into the board
    AGENT_DISCOVERED = "agent_discovered"  # a turn surfaced a new needed feature
    PLANNER = "planner"  # applied from an AI backlog-planner draft (CodeDevTaskPlan)


class CodeDevSession(db.Model):
    """One interactive Dev Mode session on a Code project.

    Binds a long-running dev container (``npm run dev`` / Vite :5173) + a
    persistent checklist (``CodeDevTask``) + a stream of bounded turn runs
    (``code_dev_turn``). Mirrors the ``CodeDeployment`` registry conventions:
    per-project container identity on the shared app network, JSON-in-Text
    ledger, lazy-reconciled status. The container lifecycle is DECOUPLED from any
    single run so it is not killed by a run finishing / the executor draining —
    only an explicit deploy / stop / idle-reap tears it down.

    ``shared_ledger_raw`` is the session-scoped consensus ledger: each turn merges
    the user's revisions here and re-reads it first (before falling back to the
    latest turn run, then the full-generation run) so multi-turn steering never
    gets clobbered by the initial full-generation ledger.
    """

    __tablename__ = "code_dev_sessions"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # NOT unique: a project can have multiple sessions over its lifetime; the
    # service layer enforces at most one ACTIVE session per (project, lane).
    project_id = db.Column(
        db.String(36), db.ForeignKey("code_projects.id"), nullable=False, index=True
    )
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    team_id = db.Column(db.String(36), db.ForeignKey("teams.id"), nullable=True, index=True)

    lane = db.Column(db.String(20), nullable=False, default="frontend")  # frontend | backend

    status = db.Column(
        db.String(20), nullable=False, default=DevSessionStatus.STARTING, index=True
    )
    health = db.Column(db.String(20), nullable=True)  # healthy / unhealthy / unknown
    restart_count = db.Column(db.Integer, nullable=False, default=0)

    # Long-running dev container identity (per-project, on the shared app network).
    container_name = db.Column(db.String(120), nullable=True)
    internal_port = db.Column(db.Integer, nullable=True)  # Vite dev server port (5173)
    workdir = db.Column(db.Text, nullable=True)  # DooD host path of the source workspace
    preview_path = db.Column(db.String(200), nullable=True)  # /preview/<pid>/

    # Which frontend/backend generation run seeded the source materialized into the container.
    base_source_run_id = db.Column(db.String(36), nullable=True)

    # Session-scoped consensus ledger (ContextLedger.to_dict) — see class docstring.
    shared_ledger_raw = db.Column(db.Text, nullable=True)

    error_message = db.Column(db.Text, nullable=True)
    # Free-form JSON: last turn id, container logs tail, heal history.
    detail_raw = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_active_at = db.Column(db.DateTime, default=datetime.utcnow)  # idle-reap basis
    stopped_at = db.Column(db.DateTime, nullable=True)

    @staticmethod
    def _load(raw, default):
        if not raw:
            return default
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return default

    def get_shared_ledger(self) -> dict:
        return self._load(self.shared_ledger_raw, {})

    def set_shared_ledger(self, data: dict | None) -> None:
        self.shared_ledger_raw = json.dumps(data or {}, ensure_ascii=False)

    def get_detail(self) -> dict:
        return self._load(self.detail_raw, {})

    def set_detail(self, data: dict | None) -> None:
        self.detail_raw = json.dumps(data or {}, ensure_ascii=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "lane": self.lane,
            "status": self.status,
            "health": self.health,
            "restart_count": self.restart_count,
            "container_name": self.container_name,
            "internal_port": self.internal_port,
            "preview_path": self.preview_path,
            "base_source_run_id": self.base_source_run_id,
            "error_message": self.error_message,
            "detail": self.get_detail(),
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "updated_at": self.updated_at.isoformat() + "Z" if self.updated_at else None,
            "last_active_at": self.last_active_at.isoformat() + "Z" if self.last_active_at else None,
            "stopped_at": self.stopped_at.isoformat() + "Z" if self.stopped_at else None,
        }


class CodeDevTask(db.Model):
    """One functional checklist item (功能点) in a Dev Mode session.

    The persistent, user-visible/editable progress board that fills the gap left
    by ``_verify_support.features_from_ledger`` (which is a per-round DERIVED list
    with no persistence). Seeded from the ledger's FR/NFR at session start, then
    advanced by turns (``apply_feature_results`` semantics, by ``feature_id``) and
    by the user directly. Writes MUST be atomic (``UPDATE ... WHERE``) — the board
    is written concurrently by multiple turns (see dev_service).
    """

    __tablename__ = "code_dev_tasks"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(
        db.String(36), db.ForeignKey("code_projects.id"), nullable=False, index=True
    )
    session_id = db.Column(
        db.String(36), db.ForeignKey("code_dev_sessions.id"), nullable=False, index=True
    )

    # Ties back to the ledger requirement id (e.g. "FR-01") so turn results fold in
    # by id. Sub-tasks use stable dotted ids ("FR1.T1" / "ASSET.FR2.1").
    feature_id = db.Column(db.String(60), nullable=True, index=True)
    # Parent FR/NFR this sub-task belongs to (e.g. "FR1"); scheduling uses it to
    # avoid running two sub-tasks of the same feature concurrently (P3).
    parent_feature_id = db.Column(db.String(60), nullable=True)
    # frontend | backend | fullstack | asset. NULL means frontend (legacy rows —
    # schema_guard adds new columns as NULLable, so treat NULL as the default).
    lane = db.Column(db.String(20), nullable=True)
    category = db.Column(db.String(20), nullable=False, default="functional")  # functional | nonfunctional | asset | chore | test
    title = db.Column(db.String(300), nullable=False)
    description = db.Column(db.Text, nullable=True)

    status = db.Column(
        db.String(20), nullable=False, default=DevTaskStatus.PENDING, index=True
    )
    source = db.Column(db.String(20), nullable=False, default=DevTaskSource.LEDGER_SEED)
    origin_turn_run_id = db.Column(db.String(36), nullable=True)  # which turn delivered/added it
    # The most recent turn run that ATTEMPTED this task (delivered or not).
    last_attempt_run_id = db.Column(db.String(36), nullable=True)
    note = db.Column(db.Text, nullable=True)  # acceptance / reviewer note
    blocked_reason = db.Column(db.Text, nullable=True)
    order_index = db.Column(db.Integer, nullable=False, default=0)

    # Scheduling knobs (NULLable for schema_guard; read through the properties).
    priority = db.Column(db.Integer, nullable=True)  # higher runs first; NULL = 0
    retry_count = db.Column(db.Integer, nullable=True)  # NULL = 0
    max_retries = db.Column(db.Integer, nullable=True)  # NULL = scheduler default

    # JSON-in-Text (access via the get_/set_ helpers, matching the model convention):
    acceptance_criteria_raw = db.Column(db.Text, nullable=True)  # list[str]
    depends_on_raw = db.Column(db.Text, nullable=True)  # list[feature_id]
    resource_spec_raw = db.Column(db.Text, nullable=True)  # asset/skill tasks: {skill, outputs}

    # Backlog-planner provenance: which plan draft produced this task (nullable —
    # manual/ledger tasks have none) + advisory planner metadata (risk / files_hint /
    # estimated_turns). Neither participates in scheduling.
    plan_id = db.Column(db.String(36), nullable=True, index=True)
    planner_meta_raw = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @staticmethod
    def _load(raw, default):
        if not raw:
            return default
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return default

    @property
    def effective_lane(self) -> str:
        return self.lane or "frontend"

    @property
    def effective_priority(self) -> int:
        return self.priority if isinstance(self.priority, int) else 0

    @property
    def effective_retry_count(self) -> int:
        return self.retry_count if isinstance(self.retry_count, int) else 0

    def get_acceptance_criteria(self) -> list:
        data = self._load(self.acceptance_criteria_raw, [])
        return [str(c) for c in data if str(c).strip()] if isinstance(data, list) else []

    def set_acceptance_criteria(self, criteria: list | None) -> None:
        self.acceptance_criteria_raw = json.dumps(
            [str(c) for c in (criteria or []) if str(c).strip()], ensure_ascii=False
        )

    def get_depends_on(self) -> list:
        data = self._load(self.depends_on_raw, [])
        return [str(d) for d in data if str(d).strip()] if isinstance(data, list) else []

    def set_depends_on(self, deps: list | None) -> None:
        self.depends_on_raw = json.dumps(
            [str(d) for d in (deps or []) if str(d).strip()], ensure_ascii=False
        )

    def get_resource_spec(self) -> dict:
        data = self._load(self.resource_spec_raw, {})
        return data if isinstance(data, dict) else {}

    def set_resource_spec(self, spec: dict | None) -> None:
        self.resource_spec_raw = json.dumps(spec or {}, ensure_ascii=False)

    def get_planner_meta(self) -> dict:
        data = self._load(self.planner_meta_raw, {})
        return data if isinstance(data, dict) else {}

    def set_planner_meta(self, meta: dict | None) -> None:
        self.planner_meta_raw = json.dumps(meta or {}, ensure_ascii=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "session_id": self.session_id,
            "feature_id": self.feature_id,
            "parent_feature_id": self.parent_feature_id,
            "lane": self.effective_lane,
            "category": self.category,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "source": self.source,
            "origin_turn_run_id": self.origin_turn_run_id,
            "last_attempt_run_id": self.last_attempt_run_id,
            "note": self.note,
            "blocked_reason": self.blocked_reason,
            "order_index": self.order_index,
            "priority": self.effective_priority,
            "retry_count": self.effective_retry_count,
            "max_retries": self.max_retries,
            "acceptance_criteria": self.get_acceptance_criteria(),
            "depends_on": self.get_depends_on(),
            "resource_spec": self.get_resource_spec(),
            "plan_id": self.plan_id,
            "planner_meta": self.get_planner_meta(),
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "updated_at": self.updated_at.isoformat() + "Z" if self.updated_at else None,
        }


class DevTaskPlanStatus:
    """Lifecycle of one backlog-planner draft (P1).

        planning -> draft -> applying -> applied
        planning -> failed
        draft -> rejected | stale        (stale = inputs changed since generation)
        applying -> failed
    """

    PLANNING = "planning"
    DRAFT = "draft"
    APPLYING = "applying"
    APPLIED = "applied"
    REJECTED = "rejected"
    STALE = "stale"
    FAILED = "failed"

    ACTIVE = {PLANNING, DRAFT, APPLYING}
    TERMINAL = {APPLIED, REJECTED, STALE, FAILED}


class CodeDevTaskPlan(db.Model):
    """One AI-generated backlog draft awaiting user confirmation (P1 planner).

    The planner NEVER writes the task board directly: it produces a normalized
    plan JSON (``plan_raw``, contract ``dev-backlog-plan.v1``) that the user can
    view / edit / reject, and only an explicit apply folds it into ``CodeDevTask``
    through the same guarded bulk-write path as ``tasks/bulk``.
    ``input_fingerprint`` pins the docs/ledger/board state the plan was derived
    from — apply refuses (without ``force``) once those inputs drift.
    """

    __tablename__ = "code_dev_task_plans"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(
        db.String(36), db.ForeignKey("code_projects.id"), nullable=False, index=True
    )
    session_id = db.Column(
        db.String(36), db.ForeignKey("code_dev_sessions.id"), nullable=False, index=True
    )
    run_id = db.Column(db.String(36), nullable=True, index=True)

    status = db.Column(
        db.String(20), nullable=False, default=DevTaskPlanStatus.PLANNING, index=True
    )
    mode = db.Column(db.String(30), nullable=False, default="from_project")
    created_by = db.Column(db.String(36), nullable=False)

    input_fingerprint = db.Column(db.String(64), nullable=True)
    target_lanes_raw = db.Column(db.Text, nullable=True)  # list[str]
    plan_raw = db.Column(db.Text, nullable=True)  # normalized plan JSON (dev-backlog-plan.v1)
    warnings_raw = db.Column(db.Text, nullable=True)  # list[str]
    error_message = db.Column(db.Text, nullable=True)

    inserted_count = db.Column(db.Integer, nullable=True)
    updated_count = db.Column(db.Integer, nullable=True)
    skipped_count = db.Column(db.Integer, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    applied_at = db.Column(db.DateTime, nullable=True)

    @staticmethod
    def _load(raw, default):
        if not raw:
            return default
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return default

    def get_target_lanes(self) -> list:
        data = self._load(self.target_lanes_raw, [])
        return [str(x) for x in data] if isinstance(data, list) else []

    def set_target_lanes(self, lanes: list | None) -> None:
        self.target_lanes_raw = json.dumps([str(x) for x in (lanes or [])], ensure_ascii=False)

    def get_plan(self) -> dict:
        data = self._load(self.plan_raw, {})
        return data if isinstance(data, dict) else {}

    def set_plan(self, plan: dict | None) -> None:
        self.plan_raw = json.dumps(plan or {}, ensure_ascii=False)

    def get_warnings(self) -> list:
        data = self._load(self.warnings_raw, [])
        return [str(x) for x in data] if isinstance(data, list) else []

    def set_warnings(self, warnings: list | None) -> None:
        self.warnings_raw = json.dumps([str(x) for x in (warnings or [])], ensure_ascii=False)

    def to_dict(self, include_plan: bool = True) -> dict:
        data = {
            "id": self.id,
            "project_id": self.project_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "status": self.status,
            "mode": self.mode,
            "created_by": self.created_by,
            "input_fingerprint": self.input_fingerprint,
            "target_lanes": self.get_target_lanes(),
            "warnings": self.get_warnings(),
            "error_message": self.error_message,
            "inserted_count": self.inserted_count,
            "updated_count": self.updated_count,
            "skipped_count": self.skipped_count,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "updated_at": self.updated_at.isoformat() + "Z" if self.updated_at else None,
            "applied_at": self.applied_at.isoformat() + "Z" if self.applied_at else None,
        }
        if include_plan:
            data["plan"] = self.get_plan()
        return data


class DevSprintStatus:
    """Lifecycle of one sprint (a bounded scheduling loop over the task backlog).

        planned -> running -> completed | blocked | failed
        running -> pausing -> paused -> running        (user pause / resume)
        running | paused | planned -> cancelled        (user cancel)
    """

    PLANNED = "planned"
    RUNNING = "running"
    PAUSING = "pausing"  # pause requested; current turn is finishing
    PAUSED = "paused"
    COMPLETED = "completed"  # every required task settled (done / skipped)
    BLOCKED = "blocked"  # no ready task but unsettled tasks remain (needs the user)
    FAILED = "failed"  # infra-level failure (repeated run crashes)
    CANCELLED = "cancelled"

    ACTIVE = {PLANNED, RUNNING, PAUSING}
    TERMINAL = {COMPLETED, BLOCKED, FAILED, CANCELLED}


class CodeDevSprint(db.Model):
    """One sprint: the scheduler's persistent cursor over a session's backlog.

    The DB is the single source of truth — the sprint row (+ the task rows) carry
    ALL scheduling state, so the orchestrating run is stateless and can be
    re-dispatched after a pause or a service restart and simply continue. The
    long-running dev container only holds the code workspace, never the backlog.
    """

    __tablename__ = "code_dev_sprints"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(
        db.String(36), db.ForeignKey("code_projects.id"), nullable=False, index=True
    )
    session_id = db.Column(
        db.String(36), db.ForeignKey("code_dev_sessions.id"), nullable=False, index=True
    )
    # The orchestrating AgentRun driving this sprint (pause/resume/cancel target).
    run_id = db.Column(db.String(36), nullable=True, index=True)

    lane = db.Column(db.String(20), nullable=False, default="frontend")
    status = db.Column(
        db.String(20), nullable=False, default=DevSprintStatus.PLANNED, index=True
    )
    mode = db.Column(db.String(20), nullable=False, default="serial")  # serial | parallel | mixed

    max_turns = db.Column(db.Integer, nullable=True)  # NULL = scheduler default
    turn_count = db.Column(db.Integer, nullable=False, default=0)
    stall_count = db.Column(db.Integer, nullable=False, default=0)  # consecutive no-progress turns

    # JSON-in-Text: latest status-count snapshot / stop reason ({counts, reason, ...}).
    last_progress_snapshot_raw = db.Column(db.Text, nullable=True)
    # JSON-in-Text: task ids currently claimed by the scheduler (serial: 0 or 1).
    current_task_ids_raw = db.Column(db.Text, nullable=True)

    created_by = db.Column(db.String(36), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    finished_at = db.Column(db.DateTime, nullable=True)

    @staticmethod
    def _load(raw, default):
        if not raw:
            return default
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return default

    def get_progress_snapshot(self) -> dict:
        data = self._load(self.last_progress_snapshot_raw, {})
        return data if isinstance(data, dict) else {}

    def set_progress_snapshot(self, data: dict | None) -> None:
        self.last_progress_snapshot_raw = json.dumps(data or {}, ensure_ascii=False)

    def get_current_task_ids(self) -> list:
        data = self._load(self.current_task_ids_raw, [])
        return [str(t) for t in data] if isinstance(data, list) else []

    def set_current_task_ids(self, ids: list | None) -> None:
        self.current_task_ids_raw = json.dumps([str(t) for t in (ids or [])], ensure_ascii=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "lane": self.lane,
            "status": self.status,
            "mode": self.mode,
            "max_turns": self.max_turns,
            "turn_count": self.turn_count,
            "stall_count": self.stall_count,
            "progress_snapshot": self.get_progress_snapshot(),
            "current_task_ids": self.get_current_task_ids(),
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "updated_at": self.updated_at.isoformat() + "Z" if self.updated_at else None,
            "finished_at": self.finished_at.isoformat() + "Z" if self.finished_at else None,
        }
