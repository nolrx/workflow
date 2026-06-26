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
