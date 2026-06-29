"""
Agent step model.

One AgentStep is one agent's turn inside a run (e.g. the Requirements Agent).
It stores both the user-facing explainable summary and the full debug trace
(prompt snapshot + model response) so the workspace can show what happened.
"""
import json
import uuid
from datetime import datetime

from backend.extensions import db


class AgentStepStatus:
    """Lifecycle status constants for an agent step."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class AgentStep(db.Model):
    """A single agent turn within an agent run."""

    __tablename__ = "agent_steps"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id = db.Column(
        db.String(36), db.ForeignKey("agent_runs.id"), nullable=False, index=True
    )
    parent_step_id = db.Column(db.String(36), nullable=True)

    agent_key = db.Column(db.String(60), nullable=False)  # stable id, e.g. "requirements"
    agent_name = db.Column(db.String(120), nullable=False)  # display name
    role = db.Column(db.String(40), nullable=True)  # planner | generator | critic | publisher
    order_index = db.Column(db.Integer, nullable=False, default=0)
    attempt = db.Column(db.Integer, nullable=False, default=1)

    status = db.Column(db.String(20), nullable=False, default=AgentStepStatus.PENDING)

    # Explainable, user-facing fields
    input_summary = db.Column(db.Text, nullable=True)
    output_summary = db.Column(db.Text, nullable=True)
    reasoning_summary = db.Column(db.Text, nullable=True)
    decision_notes = db.Column(db.Text, nullable=True)
    self_check = db.Column(db.Text, nullable=True)
    next_action = db.Column(db.Text, nullable=True)

    # Full debug trace
    model_provider = db.Column(db.String(40), nullable=True)
    model_name = db.Column(db.String(80), nullable=True)
    prompt_snapshot = db.Column(db.Text, nullable=True)
    model_response = db.Column(db.Text, nullable=True)

    # Session context ledger (internal / debug-only). ``context_snapshot_raw``
    # records the consensus block injected into THIS step's prompt plus the
    # ledger state at this point; ``context_check_raw`` records the deterministic
    # + AI consistency-gate result. Both null for steps that touch no context.
    context_snapshot_raw = db.Column(db.Text, nullable=True)
    context_check_raw = db.Column(db.Text, nullable=True)

    # Typed PortValue bindings for a composable-workflow (canvas) stage node: which
    # typed inputs this step consumed (by port → reference), which typed outputs it
    # produced, and the resolved prompt pin. Null for non-canvas / freeform steps.
    # Lets a run be replayed with its exact data lineage — see
    # docs/composable-workflow-schema.md §7.
    port_bindings_raw = db.Column(db.Text, nullable=True)

    error_message = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)

    # ---- JSON column helpers -------------------------------------------------
    @staticmethod
    def _load_json(raw, default):
        if not raw:
            return default
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return default

    def get_context_snapshot(self) -> dict:
        return self._load_json(self.context_snapshot_raw, {})

    def set_context_snapshot(self, data: dict | None) -> None:
        self.context_snapshot_raw = json.dumps(data or {}, ensure_ascii=False)

    def get_context_check(self) -> dict:
        return self._load_json(self.context_check_raw, {})

    def set_context_check(self, data: dict | None) -> None:
        self.context_check_raw = json.dumps(data or {}, ensure_ascii=False)

    def get_port_bindings(self) -> dict:
        return self._load_json(self.port_bindings_raw, {})

    def set_port_bindings(self, data: dict | None) -> None:
        self.port_bindings_raw = json.dumps(data or {}, ensure_ascii=False)

    def to_dict(self, include_debug: bool = True) -> dict:
        data = {
            "id": self.id,
            "run_id": self.run_id,
            "parent_step_id": self.parent_step_id,
            "agent_key": self.agent_key,
            "agent_name": self.agent_name,
            "role": self.role,
            "order_index": self.order_index,
            "attempt": self.attempt,
            "status": self.status,
            "input_summary": self.input_summary,
            "output_summary": self.output_summary,
            "reasoning_summary": self.reasoning_summary,
            "decision_notes": self.decision_notes,
            "self_check": self.self_check,
            "next_action": self.next_action,
            "model_provider": self.model_provider,
            "model_name": self.model_name,
            "port_bindings": self.get_port_bindings(),
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "started_at": self.started_at.isoformat() + "Z" if self.started_at else None,
            "completed_at": self.completed_at.isoformat() + "Z" if self.completed_at else None,
        }
        # Debug-only heavy trace (full prompt + model response + context-ledger
        # snapshot). Shown only in the AgentRunPanel debug tabs, so it is omitted
        # from the default lite snapshot and fetched on demand per step — these
        # fields, re-shipped on every snapshot refresh, were the other half of the
        # conversation wire cost. See GET /api/agent/runs/<id>/steps/<step_id>.
        if include_debug:
            data["prompt_snapshot"] = self.prompt_snapshot
            data["model_response"] = self.model_response
            data["context_snapshot"] = self.get_context_snapshot()
            data["context_check"] = self.get_context_check()
        return data
