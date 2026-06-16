"""
Structured types passed through the agent orchestration layer.

Agents receive an ``AgentContext`` (identity + config + cancel check) and a
``RunRecorder`` (to emit steps / events / artifacts). Keeping these explicit
avoids passing bare strings around between agents.
"""
from dataclasses import dataclass, field
from typing import Callable, Optional


def _never_cancelled() -> bool:
    return False


@dataclass
class AgentContext:
    """Immutable execution context handed to a workflow and its agents."""

    run_id: str
    user_id: str
    team_id: Optional[str]
    domain: str
    workflow: str
    resource_type: Optional[str]
    resource_id: Optional[str]
    config: dict = field(default_factory=dict)
    input_snapshot: dict = field(default_factory=dict)
    # Set by the runtime; returns True once the run is asked to cancel.
    is_cancelled: Callable[[], bool] = _never_cancelled
