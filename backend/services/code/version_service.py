"""
Per-stage version history service for Code projects.

Every write to a versionable stage product (whether from an agent-swarm step, a
standalone generation route, or a manual edit) funnels through
``record_stage_version`` so the history table stays an accurate, append-only
trail. ``activate_stage_version`` is the rollback path: it rewrites the live
``CodeProject`` (rebuilding ``CodeDocument`` rows for the documents stage) from a
chosen historical version and moves the ``is_current`` pointer.

Invariant: whenever a stage's live content on ``CodeProject`` changes, exactly one
``CodeStageVersion`` row for that ``(project, stage)`` is ``is_current = True`` and
holds the same content. Version recording is best-effort (``safe_*`` wrappers) so a
history hiccup never breaks the primary generation/edit; rollback surfaces errors.
"""
import json
import logging

from sqlalchemy import func

from backend.extensions import db
from backend.models.code import (
    CodeDocument,
    CodeStage,
    CodeStageVersion,
    CodeStageVersionSource,
)

logger = logging.getLogger(__name__)

# Manual-edit fields on CodeProject -> the stage trail they belong to.
STAGE_FOR_FIELD = {
    "requirements_doc": CodeStage.REQUIREMENTS,
    "development_flow": CodeStage.FLOW,
    "style_prompt": CodeStage.STYLE,
    "confirmed_preview_url": CodeStage.PREVIEW,
    "ui_baseline_prompt": CodeStage.PREVIEW,
}


def _summarize_text(text: str | None, limit: int = 120) -> str:
    """First non-empty, heading-stripped line of a markdown/text product."""
    for line in (text or "").splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:limit]
    return (text or "").strip()[:limit]


def _capture_stage(project, stage: str):
    """Read the project's current content for ``stage``.

    Returns ``(content_text, content_json, summary)``.
    """
    if stage == CodeStage.REQUIREMENTS:
        return project.requirements_doc, None, _summarize_text(project.requirements_doc)
    if stage == CodeStage.FLOW:
        return project.development_flow, None, _summarize_text(project.development_flow)
    if stage == CodeStage.STYLE:
        return (
            project.style_prompt,
            {"selected_style_ids": project.get_selected_style_ids()},
            _summarize_text(project.style_prompt),
        )
    if stage == CodeStage.DOCUMENTS:
        docs = [
            {
                "document_type": doc.document_type,
                "title": doc.title,
                "content": doc.content,
                "prompt_expert": doc.prompt_expert,
                "order_index": doc.order_index,
            }
            for doc in project.documents.all()
        ]
        return None, {"documents": docs}, f"{len(docs)} 篇文档"
    if stage == CodeStage.PREVIEW:
        images = project.get_preview_images()
        payload = {
            "preview_images": images,
            "confirmed_preview_url": project.confirmed_preview_url,
            "ui_baseline_prompt": project.ui_baseline_prompt,
        }
        return None, payload, f"{len(images)} 张预览图"
    raise ValueError(f"unknown stage: {stage}")


def _has_content(stage: str, content_text, content_json) -> bool:
    """Whether the captured content is worth versioning (skip empty stages)."""
    if (content_text or "").strip():
        return True
    if stage == CodeStage.DOCUMENTS:
        return bool((content_json or {}).get("documents"))
    if stage == CodeStage.PREVIEW:
        data = content_json or {}
        return bool(
            data.get("preview_images")
            or data.get("confirmed_preview_url")
            or data.get("ui_baseline_prompt")
        )
    if stage == CodeStage.STYLE:
        # style text was empty above; the style-id selection alone isn't a product.
        return False
    return False


def _signature(content_text, content_json) -> str:
    """Stable fingerprint for de-duplicating identical consecutive versions."""
    if content_text is not None:
        return "text:" + content_text
    return "json:" + json.dumps(content_json or {}, ensure_ascii=False, sort_keys=True)


def _latest(project_id: str, stage: str) -> CodeStageVersion | None:
    return (
        CodeStageVersion.query.filter_by(project_id=project_id, stage=stage)
        .order_by(CodeStageVersion.version_number.desc())
        .first()
    )


def _mark_current(project_id: str, stage: str, version: CodeStageVersion) -> None:
    """Make ``version`` the sole current version for its (project, stage)."""
    CodeStageVersion.query.filter_by(project_id=project_id, stage=stage).update(
        {"is_current": False}
    )
    version.is_current = True


def record_stage_version(
    project,
    stage: str,
    *,
    source: str = CodeStageVersionSource.GENERATED,
    run_id: str | None = None,
    step_id: str | None = None,
    note: str | None = None,
    dedup: bool = True,
) -> CodeStageVersion | None:
    """Snapshot the project's current ``stage`` content as a new current version.

    Skips empty stages. When ``dedup`` and the newest existing version already holds
    identical content, no new row is created (the existing one is kept current).
    Commits its own work. Returns the created/reused version, or None if skipped.
    """
    if stage not in CodeStage.ALL:
        raise ValueError(f"unknown stage: {stage}")

    content_text, content_json, summary = _capture_stage(project, stage)
    if not _has_content(stage, content_text, content_json):
        return None

    latest = _latest(project.id, stage)
    if (
        dedup
        and latest is not None
        and _signature(latest.content_text, latest.get_content_json())
        == _signature(content_text, content_json)
    ):
        if not latest.is_current:
            _mark_current(project.id, stage, latest)
            db.session.commit()
        return latest

    max_version = (
        db.session.query(func.max(CodeStageVersion.version_number))
        .filter_by(project_id=project.id, stage=stage)
        .scalar()
        or 0
    )
    version = CodeStageVersion(
        project_id=project.id,
        stage=stage,
        version_number=max_version + 1,
        source=source,
        content_text=content_text,
        summary=summary,
        run_id=run_id,
        step_id=step_id,
        note=note,
    )
    version.set_content_json(content_json)
    _mark_current(project.id, stage, version)
    db.session.add(version)
    db.session.commit()
    return version


def safe_record_stage_version(project, stage: str, **kwargs) -> CodeStageVersion | None:
    """Best-effort ``record_stage_version``; logs and swallows any failure.

    The primary product is already persisted on CodeProject, so a version-history
    hiccup must never fail the generation/edit that produced it.
    """
    try:
        return record_stage_version(project, stage, **kwargs)
    except Exception:  # noqa: BLE001 — history is auxiliary, never fatal
        logger.warning(
            "record stage version failed: project=%s stage=%s",
            getattr(project, "id", "?"),
            stage,
            exc_info=True,
        )
        db.session.rollback()
        return None


def record_versions_for_fields(
    project, fields, *, source: str = CodeStageVersionSource.MANUAL_EDIT
) -> None:
    """After a manual edit, snapshot each stage whose field(s) changed (deduped)."""
    seen = set()
    for field in fields:
        stage = STAGE_FOR_FIELD.get(field)
        if stage and stage not in seen:
            seen.add(stage)
            safe_record_stage_version(project, stage, source=source)


def list_stage_versions(project, stage: str, *, seed: bool = True) -> list[CodeStageVersion]:
    """All versions for a stage, newest first.

    When ``seed`` and a stage has live content but no recorded versions yet (a
    project created before history existed), a baseline ``import`` version is
    lazily created so the trail is never empty for existing content.
    """
    if stage not in CodeStage.ALL:
        raise ValueError(f"unknown stage: {stage}")
    versions = (
        CodeStageVersion.query.filter_by(project_id=project.id, stage=stage)
        .order_by(CodeStageVersion.version_number.desc())
        .all()
    )
    if not versions and seed:
        seeded = safe_record_stage_version(
            project, stage, source=CodeStageVersionSource.IMPORT, dedup=False
        )
        if seeded is not None:
            versions = [seeded]
    return versions


def get_stage_version(project, stage: str, version_id: str) -> CodeStageVersion | None:
    return CodeStageVersion.query.filter_by(
        id=version_id, project_id=project.id, stage=stage
    ).first()


def _apply_version_to_project(project, stage: str, version: CodeStageVersion) -> None:
    """Materialize a historical version's content back onto the live project."""
    if stage == CodeStage.REQUIREMENTS:
        project.requirements_doc = version.content_text
    elif stage == CodeStage.FLOW:
        project.development_flow = version.content_text
    elif stage == CodeStage.STYLE:
        project.style_prompt = version.content_text
        data = version.get_content_json() or {}
        if isinstance(data.get("selected_style_ids"), list):
            project.set_selected_style_ids(data["selected_style_ids"])
    elif stage == CodeStage.DOCUMENTS:
        data = version.get_content_json() or {}
        documents = data.get("documents") or []
        # project.documents has order_by, so .delete() on it raises under
        # SQLAlchemy 2.x; delete via a plain query (mirrors the workflow).
        CodeDocument.query.filter_by(project_id=project.id).delete()
        for index, item in enumerate(documents):
            db.session.add(
                CodeDocument(
                    project_id=project.id,
                    document_type=item.get("document_type") or "document",
                    title=item.get("title") or "",
                    content=item.get("content") or "",
                    prompt_expert=item.get("prompt_expert") or "",
                    order_index=item.get("order_index", index) or index,
                )
            )
    elif stage == CodeStage.PREVIEW:
        data = version.get_content_json() or {}
        project.set_preview_images(data.get("preview_images") or [])
        if "confirmed_preview_url" in data:
            project.confirmed_preview_url = data.get("confirmed_preview_url")
        if "ui_baseline_prompt" in data:
            project.ui_baseline_prompt = data.get("ui_baseline_prompt")
    else:
        raise ValueError(f"unknown stage: {stage}")


def activate_stage_version(project, stage: str, version: CodeStageVersion) -> CodeStageVersion:
    """Roll back: restore ``version``'s content to the project and make it current."""
    _apply_version_to_project(project, stage, version)
    _mark_current(project.id, stage, version)
    db.session.commit()
    return version
