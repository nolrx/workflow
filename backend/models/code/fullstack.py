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
