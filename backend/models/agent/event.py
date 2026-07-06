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
    # Session context ledger lifecycle (internal / debug-only observability).
    CONTEXT_UPDATED = "context_updated"
    CONTEXT_CONFLICT = "context_conflict"
    WARNING = "warning"
    ERROR = "error"
    STEP_COMPLETED = "step_completed"
    RUN_COMPLETED = "run_completed"
    # Human-in-the-loop review lifecycle (Code-domain step confirmation). A
    # generator stage produces its document, then the run pauses on
    # STEP_AWAITING_REVIEW until the user either submits a USER_REVISION
    # (adjustment instruction → regenerate) or approves (REVIEW_RESOLVED → advance).
    STEP_AWAITING_REVIEW = "step_awaiting_review"
    USER_REVISION = "user_revision"
    REVIEW_RESOLVED = "review_resolved"
    # GitHub auto-sync lifecycle (emitted at the tail of a completed code run).
    # payload.status is one of pending | success | failed.
    GITHUB_SYNC = "github_sync"
    # Dev Mode (交互式开发模式) lifecycle. CHECKLIST_UPDATED carries the current
    # functional checklist board (payload.board) so the right-pane progress window
    # updates live; DEV_PREVIEW_READY signals the dev server is serving (payload.url)
    # so the iframe can (re)load; DEV_CONTAINER_HEALTH reports container liveness /
    # self-heal (payload.status). Emitted by the code_dev_turn workflow / dev_service.
    CHECKLIST_UPDATED = "checklist_updated"
    DEV_PREVIEW_READY = "dev_preview_ready"
    DEV_CONTAINER_HEALTH = "dev_container_health"


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

    # Heavy free-text keys that some emit sites historically stored in the event
    # payload (full prompt / model response / summary / injected context block).
    # The client never reads them — the authoritative copy lives on the step — so a
    # ``slim`` snapshot drops them to cut wire size. Matters for runs persisted
    # before the recorder stopped writing the prompt/response (new runs omit them
    # at write time); harmless for new runs that never had them.
    _HEAVY_PAYLOAD_KEYS = ("prompt", "response", "summary", "injected_text")

    def get_payload(self) -> dict:
        if not self.payload_raw:
            return {}
        try:
            return json.loads(self.payload_raw)
        except (json.JSONDecodeError, TypeError):
            return {}

    def set_payload(self, data: dict | None) -> None:
        self.payload_raw = json.dumps(data or {}, ensure_ascii=False)

    def to_dict(self, slim: bool = False) -> dict:
        payload = self.get_payload()
        if slim and payload:
            payload = {k: v for k, v in payload.items() if k not in self._HEAVY_PAYLOAD_KEYS}
        return {
            "id": self.id,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "level": self.level,
            "message": self.message,
            "payload": payload,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
        }
