"""
Shared helpers that make the frontend/backend generation lane workflows
ITERATION-AWARE (二次开发·真实续改).

When a lane run is started from an App Space iteration (``config.iteration_id``
present, set by ``apps_routes.confirm_iteration``), these helpers:
  * load the iteration's change instruction + plan (so it flows into the prompt), and
  * load the project's latest published source zip for that lane,

so the lane service can SEED the existing project and EDIT it in place instead of
regenerating from scratch. Falls back cleanly (no iteration / no prior source) to
the original from-scratch behaviour.
"""
import logging
import zipfile

from backend.models.agent import AgentArtifact
from backend.services.agent.files import artifact_abs_path

logger = logging.getLogger(__name__)

# Prefer the deploy-repaired source over the original generation source so a
#续改 builds on the latest *deployed* code, not a stale first generation.
_LANE_SOURCE_REFS = {
    "backend": ["code_backend_project_repaired_zip", "code_backend_project_zip"],
    "frontend": ["code_frontend_project_zip"],
}


def iteration_change(ctx) -> dict | None:
    """Return ``{instruction, plan_text}`` when this run is an iteration, else None."""
    iteration_id = (ctx.config or {}).get("iteration_id")
    if not iteration_id:
        return None
    # Imported lazily to avoid importing the Code models at workflow-module load.
    from backend.extensions import db
    from backend.models.code.fullstack import CodeAppIteration

    iteration = db.session.get(CodeAppIteration, iteration_id)
    if not iteration:
        return None
    plan = iteration.get_plan() or {}
    steps = plan.get("steps") or []
    plan_text = "\n".join(
        f"- [{s.get('lane')}] {s.get('action')}: {s.get('description')}"
        for s in steps
        if isinstance(s, dict)
    )
    return {
        "iteration_id": iteration_id,
        "instruction": iteration.instruction or "",
        "plan_text": plan_text,
    }


def _load_zip(artifact: AgentArtifact) -> dict:
    """Load a source-zip artifact into ``{relpath: bytes}``; {} on any problem."""
    if not artifact or not artifact.storage_path:
        return {}
    try:
        abs_path = artifact_abs_path(artifact.storage_path)
        out: dict[str, bytes] = {}
        with zipfile.ZipFile(abs_path, "r") as archive:
            for name in archive.namelist():
                if name.endswith("/"):
                    continue
                out[name] = archive.read(name)
        return out
    except Exception:  # noqa: BLE001 — corrupt/missing zip → fall back to from-scratch
        logger.warning("iteration: failed to load prior source zip %s", getattr(artifact, "id", "?"))
        return {}


def load_prior_source(project_id: str, lane: str) -> dict:
    """Latest published source for a lane as ``{relpath: bytes}`` (or {} if none)."""
    for ref in _LANE_SOURCE_REFS.get(lane, []):
        artifact = (
            AgentArtifact.query.filter_by(domain_ref_type=ref, domain_ref_id=project_id)
            .order_by(AgentArtifact.created_at.desc())
            .first()
        )
        files = _load_zip(artifact)
        if files:
            return files
    return {}
