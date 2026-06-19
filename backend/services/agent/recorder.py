"""
Run recorder — persists steps / events / artifacts and pushes them live.

The recorder is the only writer of the agent observability tables. It assigns
the monotonic per-run event ``sequence`` (a run executes in a single worker
thread, so a simple in-process counter is race-free), redacts secrets before
anything is stored, and publishes each persisted event to the live bus.
"""
import logging
import re
import threading
from contextlib import contextmanager
from datetime import datetime

from backend.extensions import db
from backend.models.agent import (
    AgentArtifact,
    AgentEvent,
    AgentEventLevel,
    AgentEventType,
    AgentStep,
    AgentStepStatus,
)
from backend.services.agent.bus import event_bus
from backend.services.agent.files import save_artifact_file

logger = logging.getLogger(__name__)


# --- Secret redaction --------------------------------------------------------
# Debug traces show full prompts and model responses, but must never leak
# credentials. These patterns scrub the obvious shapes before persistence.
_REDACT_PATTERNS = [
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{6,}", re.IGNORECASE), r"\1[REDACTED]"),
    (
        re.compile(
            r"(\"?(?:api[_-]?key|authorization|access[_-]?token|refresh[_-]?token|secret|password)\"?\s*[:=]\s*\"?)"
            r"[A-Za-z0-9._\-]{6,}",
            re.IGNORECASE,
        ),
        r"\1[REDACTED]",
    ),
    (re.compile(r"\bAIza[0-9A-Za-z._\-]{10,}\b"), "[REDACTED_KEY]"),
    (re.compile(r"\bsk-[A-Za-z0-9._\-]{10,}\b"), "[REDACTED_KEY]"),
]


def redact_text(value):
    if value is None:
        return None
    text = str(value)
    for pattern, replacement in _REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_payload(payload):
    if isinstance(payload, dict):
        return {key: redact_payload(val) for key, val in payload.items()}
    if isinstance(payload, list):
        return [redact_payload(item) for item in payload]
    if isinstance(payload, str):
        return redact_text(payload)
    return payload


class StepHandle:
    """Mutating view of a single in-flight AgentStep used inside a workflow."""

    def __init__(self, recorder: "RunRecorder", step: AgentStep):
        self._recorder = recorder
        self.step = step

    @property
    def id(self) -> str:
        return self.step.id

    def model_tracer(self):
        """Return an ``on_model_call`` callback that captures prompt + response."""

        def _trace(prompt=None, text=None, success=True, error=None, provider=None, model=None):
            self.step.prompt_snapshot = redact_text(prompt)
            self.step.model_response = redact_text(text)
            if provider:
                self.step.model_provider = str(provider)
            if model:
                self.step.model_name = str(model)
            db.session.commit()
            self._recorder.emit(
                AgentEventType.MODEL_REQUEST,
                step_id=self.step.id,
                message=f"调用模型 {provider or ''} {model or ''}".strip(),
                payload={"prompt": prompt, "provider": provider, "model": model},
            )
            self._recorder.emit(
                AgentEventType.MODEL_RESPONSE,
                level=AgentEventLevel.INFO if success else AgentEventLevel.WARNING,
                step_id=self.step.id,
                message="模型返回成功" if success else f"模型返回失败: {error}",
                payload={"response": text, "success": success, "error": error},
            )

        return _trace

    def model_delta_tracer(self):
        """Return an ``on_delta`` callback that live-pushes streamed token chunks.

        Deltas are transient (live-only, never persisted): the full response is
        still stored once via ``model_tracer`` for reconnect/debug. This keeps
        the high-frequency token stream out of the events table.
        """

        def _on_delta(text):
            if text:
                self._recorder.emit_delta(self.step.id, text)

        return _on_delta

    def set_output(
        self,
        output_summary=None,
        reasoning_summary=None,
        decision_notes=None,
        self_check=None,
        next_action=None,
    ):
        if output_summary is not None:
            self.step.output_summary = output_summary
        if reasoning_summary is not None:
            self.step.reasoning_summary = reasoning_summary
        if decision_notes is not None:
            self.step.decision_notes = decision_notes
        if self_check is not None:
            self.step.self_check = self_check
        if next_action is not None:
            self.step.next_action = next_action
        db.session.commit()

    def set_context(self, snapshot=None, check=None):
        """Persist this step's context-ledger snapshot / verification result.

        Internal / debug-only: surfaced in the AgentRunPanel "Context" tab, never
        in user-facing output. See backend/services/agent/context_verifier.py.
        """
        if snapshot is not None:
            self.step.set_context_snapshot(snapshot)
        if check is not None:
            self.step.set_context_check(check)
        db.session.commit()

    def add_artifact(
        self,
        artifact_type: str,
        title: str,
        content_text=None,
        content_json=None,
        filename=None,
        mime_type=None,
        preview_url=None,
        write_file: bool = False,
        domain_ref_type=None,
        domain_ref_id=None,
        content_bytes=None,
    ) -> AgentArtifact:
        artifact = AgentArtifact(
            run_id=self._recorder.run_id,
            step_id=self.step.id,
            artifact_type=artifact_type,
            title=title,
            filename=filename,
            mime_type=mime_type,
            preview_url=preview_url,
            content_text=content_text,
            domain_ref_type=domain_ref_type,
            domain_ref_id=domain_ref_id,
        )
        artifact.set_content_json(content_json)

        # Persist an on-disk copy when asked. Supports raw bytes (e.g. a zip of a
        # generated multi-file project) as well as inline text.
        if write_file and (content_bytes is not None or content_text is not None):
            default_name = filename or f"{artifact_type}.bin"
            data = content_bytes if content_bytes is not None else content_text.encode("utf-8")
            relative = save_artifact_file(
                self._recorder.run_id,
                self.step.id,
                default_name,
                data,
            )
            artifact.storage_path = relative
            if not artifact.filename:
                artifact.filename = default_name

        db.session.add(artifact)
        db.session.commit()
        self._recorder.emit(
            AgentEventType.ARTIFACT_CREATED,
            step_id=self.step.id,
            message=f"产出: {title}",
            payload={
                "artifact_id": artifact.id,
                "artifact_type": artifact_type,
                "title": title,
            },
        )
        return artifact

    def mark_failed(self, message: str):
        self.step.status = AgentStepStatus.FAILED
        self.step.error_message = message
        db.session.commit()
        self._recorder.emit(
            AgentEventType.ERROR,
            level=AgentEventLevel.ERROR,
            step_id=self.step.id,
            message=f"{self.step.agent_name} 失败: {message}",
            payload={"error": message},
        )

    def mark_skipped(self, message: str):
        self.step.status = AgentStepStatus.SKIPPED
        db.session.commit()
        self._recorder.emit(
            AgentEventType.WARNING,
            level=AgentEventLevel.WARNING,
            step_id=self.step.id,
            message=f"{self.step.agent_name} 跳过: {message}",
            payload={"reason": message},
        )


class RunRecorder:
    """Persists and live-publishes everything that happens inside one run."""

    def __init__(self, run_id: str, bus=event_bus):
        self.run_id = run_id
        self._bus = bus
        # Resume-safe sequencing: continue past any events already persisted for
        # this run. A paused run that resumes builds a fresh recorder, and
        # restarting the counter from 0 would collide with the existing event log
        # and break the client's sequence-based dedup / ordering.
        last = (
            db.session.query(db.func.max(AgentEvent.sequence))
            .filter(AgentEvent.run_id == run_id)
            .scalar()
        )
        self._sequence = int(last or 0)
        self._lock = threading.Lock()

    def _next_sequence(self) -> int:
        with self._lock:
            self._sequence += 1
            return self._sequence

    def emit(
        self,
        event_type: str,
        level: str = AgentEventLevel.INFO,
        message=None,
        payload=None,
        step_id=None,
    ) -> AgentEvent:
        event = AgentEvent(
            run_id=self.run_id,
            step_id=step_id,
            sequence=self._next_sequence(),
            event_type=event_type,
            level=level,
            message=redact_text(message),
        )
        event.set_payload(redact_payload(payload) if payload is not None else None)
        db.session.add(event)
        db.session.commit()
        self._bus.publish(self.run_id, event.to_dict())
        return event

    def emit_delta(self, step_id: str, text: str) -> None:
        """Publish a transient token-delta to live subscribers only.

        Unlike ``emit``, this is NOT persisted and carries no sequence: token
        deltas are high-frequency and live-only. The authoritative full text is
        stored on the step (via the model tracer) and recovered on reconnect, so
        dropped deltas only degrade the live typing effect, never correctness.
        """
        self._bus.publish(self.run_id, {"kind": "delta", "step_id": step_id, "text": text})

    @contextmanager
    def step(self, agent_key, agent_name, role, order_index, input_summary=None):
        step = AgentStep(
            run_id=self.run_id,
            agent_key=agent_key,
            agent_name=agent_name,
            role=role,
            order_index=order_index,
            status=AgentStepStatus.RUNNING,
            input_summary=input_summary,
            started_at=datetime.utcnow(),
        )
        db.session.add(step)
        db.session.commit()
        self.emit(
            AgentEventType.STEP_STARTED,
            step_id=step.id,
            message=f"{agent_name} 开始",
            payload={"agent_key": agent_key, "order_index": order_index, "role": role},
        )
        handle = StepHandle(self, step)
        try:
            yield handle
        except Exception as exc:  # noqa: BLE001 - record then re-raise
            step.status = AgentStepStatus.FAILED
            step.error_message = str(exc)
            step.completed_at = datetime.utcnow()
            db.session.commit()
            self.emit(
                AgentEventType.ERROR,
                level=AgentEventLevel.ERROR,
                step_id=step.id,
                message=f"{agent_name} 异常: {exc}",
                payload={"error": str(exc)},
            )
            raise
        else:
            step.completed_at = datetime.utcnow()
            if step.status == AgentStepStatus.RUNNING:
                step.status = AgentStepStatus.COMPLETED
                db.session.commit()
                self.emit(
                    AgentEventType.STEP_COMPLETED,
                    step_id=step.id,
                    message=f"{agent_name} 完成",
                    payload={"agent_key": agent_key},
                )
            else:
                # Body already set a terminal status (failed / skipped) without raising.
                db.session.commit()
