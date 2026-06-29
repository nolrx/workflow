"""
Agent Swarm run model.

An AgentRun is a single orchestrated multi-agent execution for the Code
product domain. It owns the steps, events and artifacts produced while the
workflow runs in a background thread.
"""
import json
import uuid
from datetime import datetime

from backend.extensions import db


class AgentRunStatus:
    """Lifecycle status constants for an agent run."""

    QUEUED = "queued"
    RUNNING = "running"
    # Paused mid-run awaiting user confirmation of a produced document
    # (human-in-the-loop step review). Non-terminal: a resume restarts the worker
    # from the persisted cursor. No DB migration needed — the cursor / review
    # stage live in the existing progress JSON.
    PAUSED = "paused"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"

    # Active = occupies a run slot / may still progress. Paused counts as active
    # (the session is in-flight, just waiting on the user).
    ACTIVE = {QUEUED, RUNNING, PAUSED}
    # In-flight = ACTUALLY executing (holds a worker thread). EXCLUDES paused: a run
    # waiting on a human-in-the-loop review holds no worker, so it must NOT count
    # against the per-user concurrency cap — otherwise an abandoned paused run (e.g. a
    # canvas blueprint left at a review gate) blocks the user from starting new work.
    IN_FLIGHT = {QUEUED, RUNNING}
    TERMINAL = {COMPLETED, PARTIAL, FAILED, CANCELLED}


class AgentRun(db.Model):
    """A single agent-swarm workflow execution."""

    __tablename__ = "agent_runs"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    team_id = db.Column(db.String(36), db.ForeignKey("teams.id"), nullable=True, index=True)

    domain = db.Column(db.String(20), nullable=False)  # code
    workflow = db.Column(db.String(60), nullable=False)  # e.g. code_full_generation
    resource_type = db.Column(db.String(40), nullable=True)  # e.g. code_project
    resource_id = db.Column(db.String(36), nullable=True, index=True)

    title = db.Column(db.String(300), nullable=True)
    status = db.Column(
        db.String(20), nullable=False, default=AgentRunStatus.QUEUED, index=True
    )

    input_snapshot_raw = db.Column(db.Text, nullable=True)
    config_raw = db.Column(db.Text, nullable=True)
    progress_raw = db.Column(db.Text, nullable=True)
    # Session context ledger — the evolving, validated "consensus" (tech stack,
    # glossary, key decisions, scope) shared across all steps of this run. Never
    # shown to end users; surfaced only in the debug Context tab. See
    # docs/agent-context-ledger.md.
    context_ledger_raw = db.Column(db.Text, nullable=True)

    credit_reserved = db.Column(db.Integer, nullable=False, default=0)
    credit_used = db.Column(db.Integer, nullable=False, default=0)

    error_message = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)

    steps = db.relationship(
        "AgentStep",
        backref="run",
        lazy="dynamic",
        cascade="all, delete-orphan",
        order_by="AgentStep.order_index",
    )
    events = db.relationship(
        "AgentEvent",
        backref="run",
        lazy="dynamic",
        cascade="all, delete-orphan",
        order_by="AgentEvent.sequence",
    )
    artifacts = db.relationship(
        "AgentArtifact",
        backref="run",
        lazy="dynamic",
        cascade="all, delete-orphan",
        order_by="AgentArtifact.created_at",
    )

    # ---- JSON column helpers -------------------------------------------------
    @staticmethod
    def _load_json(raw, default):
        if not raw:
            return default
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return default

    def get_input_snapshot(self) -> dict:
        return self._load_json(self.input_snapshot_raw, {})

    def set_input_snapshot(self, data: dict | None) -> None:
        self.input_snapshot_raw = json.dumps(data or {}, ensure_ascii=False)

    def get_config(self) -> dict:
        return self._load_json(self.config_raw, {})

    def set_config(self, data: dict | None) -> None:
        self.config_raw = json.dumps(data or {}, ensure_ascii=False)

    def get_progress(self) -> dict:
        return self._load_json(
            self.progress_raw,
            {"total_steps": 0, "completed_steps": 0, "failed_steps": 0, "current_step": None},
        )

    def set_progress(self, data: dict | None) -> None:
        self.progress_raw = json.dumps(data or {}, ensure_ascii=False)

    def get_context_ledger(self) -> dict:
        return self._load_json(self.context_ledger_raw, {})

    def set_context_ledger(self, data: dict | None) -> None:
        self.context_ledger_raw = json.dumps(data or {}, ensure_ascii=False)

    # ---- Serialization -------------------------------------------------------
    def to_dict(
        self,
        include_children: bool = False,
        include_step_debug: bool = True,
        slim_events: bool = False,
    ) -> dict:
        data = {
            "id": self.id,
            "user_id": self.user_id,
            "team_id": self.team_id,
            "domain": self.domain,
            "workflow": self.workflow,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "title": self.title,
            "status": self.status,
            "input_snapshot": self.get_input_snapshot(),
            "config": self.get_config(),
            "progress": self.get_progress(),
            "context_ledger": self.get_context_ledger(),
            "credit_reserved": self.credit_reserved,
            "credit_used": self.credit_used,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "started_at": self.started_at.isoformat() + "Z" if self.started_at else None,
            "completed_at": self.completed_at.isoformat() + "Z" if self.completed_at else None,
        }
        if include_children:
            data["steps"] = [
                step.to_dict(include_debug=include_step_debug) for step in self.steps.all()
            ]
            data["events"] = [event.to_dict(slim=slim_events) for event in self.events.all()]
            data["artifacts"] = [artifact.to_dict() for artifact in self.artifacts.all()]
        return data
