"""
Agent event model.

Events are the append-only log of everything that happens inside a run. They
are the source of truth for both the SSE stream (pushed live) and the run
detail snapshot (replayed on reconnect, ordered by ``sequence``).
"""
import json
import uuid
from datetime import datetime

from backend.extensions import db


class AgentEventType:
    """Event type constants pushed over SSE / stored in the log."""

    RUN_STARTED = "run_started"
    STEP_STARTED = "step_started"
    MODEL_REQUEST = "model_request"
    MODEL_RESPONSE = "model_response"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    ARTIFACT_CREATED = "artifact_created"
    FILE_CREATED = "file_created"
    PROGRESS = "progress"
    WARNING = "warning"
    ERROR = "error"
    STEP_COMPLETED = "step_completed"
    RUN_COMPLETED = "run_completed"


class AgentEventLevel:
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class AgentEvent(db.Model):
    """A single ordered event inside an agent run."""

    __tablename__ = "agent_events"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id = db.Column(
        db.String(36), db.ForeignKey("agent_runs.id"), nullable=False, index=True
    )
    step_id = db.Column(db.String(36), nullable=True, index=True)

    # Monotonic per-run ordering, assigned by the recorder.
    sequence = db.Column(db.Integer, nullable=False, default=0)

    event_type = db.Column(db.String(40), nullable=False)
    level = db.Column(db.String(10), nullable=False, default=AgentEventLevel.INFO)
    message = db.Column(db.Text, nullable=True)
    payload_raw = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def get_payload(self) -> dict:
        if not self.payload_raw:
            return {}
        try:
            return json.loads(self.payload_raw)
        except (json.JSONDecodeError, TypeError):
            return {}

    def set_payload(self, data: dict | None) -> None:
        self.payload_raw = json.dumps(data or {}, ensure_ascii=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "level": self.level,
            "message": self.message,
            "payload": self.get_payload(),
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
        }
