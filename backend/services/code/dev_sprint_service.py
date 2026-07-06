"""
Dev Mode sprint scheduler core — the persistent task state machine.

The DB is the single source of truth (no pub/sub, no in-container backlog): the
``CodeDevTask`` rows carry the full lifecycle and every transition here is an
ATOMIC conditional UPDATE (``UPDATE ... WHERE status IN (expected)``), so a claim
can never double-fire across threads/processes — the same guard philosophy as
``credit_service.deduct_credits`` and the checklist's done-flips. ``ready`` is a
DERIVED state (pending + all depends_on done), never stored.

This module is deliberately import-light (models + stdlib only, no Docker / AI
imports) so the whole state machine is unit-testable without containers. The
orchestration loop lives in ``workflows/code_dev_sprint_workflow.py``; the
per-turn acceptance hooks live in ``workflows/code_dev_turn_workflow.py``.
Comments in English to match the Code/core convention.
"""
import logging
import os
from datetime import datetime

from backend.extensions import db
from backend.models.code.fullstack import CodeDevTask, DevTaskStatus

logger = logging.getLogger(__name__)

# Lanes a sprint of a given session lane may schedule. Asset tasks run inside the
# frontend dev container (the image-assets skill lives there), so they ride the
# frontend lane.
_LANES_FOR_SESSION = {
    "frontend": {"frontend", "asset"},
    "backend": {"backend"},
}

_VALID_CATEGORIES = {"functional", "nonfunctional", "asset", "chore", "test"}
_VALID_LANES = {"frontend", "backend", "fullstack", "asset"}


def task_default_max_retries() -> int:
    """Per-task retry budget when the task doesn't set its own (env-tunable)."""
    try:
        return max(0, int(os.getenv("CODE_DEV_TASK_MAX_RETRIES", "2")))
    except (TypeError, ValueError):
        return 2


def sprint_default_max_turns() -> int:
    """Sprint-wide turn budget (each scheduled turn run counts once)."""
    try:
        return max(1, int(os.getenv("CODE_DEV_SPRINT_MAX_TURNS", "24")))
    except (TypeError, ValueError):
        return 24


def sprint_stall_limit() -> int:
    """Consecutive turns with no newly-done task before the sprint blocks."""
    try:
        return max(1, int(os.getenv("CODE_DEV_SPRINT_STALL", "4")))
    except (TypeError, ValueError):
        return 4


def max_retries_of(task: CodeDevTask) -> int:
    return task.max_retries if isinstance(task.max_retries, int) else task_default_max_retries()


def normalize_category(raw) -> str:
    cat = str(raw or "functional").strip().lower()
    return cat if cat in _VALID_CATEGORIES else "functional"


def normalize_lane(raw) -> str | None:
    if raw is None or not str(raw).strip():
        return None
    lane = str(raw).strip().lower()
    return lane if lane in _VALID_LANES else None


# --- atomic transitions -------------------------------------------------------
def _transition(task_id: str, from_statuses, to_status: str, **fields) -> bool:
    """Atomically move a task ``from_statuses -> to_status``; True when it fired.

    ``query.update`` bypasses the ORM ``onupdate`` hook, so ``updated_at`` is set
    explicitly. ``rowcount == 0`` means someone else moved the row first — the
    caller must treat that as "lost the race", never retry blindly.
    """
    values = {CodeDevTask.status: to_status, CodeDevTask.updated_at: datetime.utcnow()}
    for key, val in fields.items():
        values[getattr(CodeDevTask, key)] = val
    updated = (
        db.session.query(CodeDevTask)
        .filter(CodeDevTask.id == task_id, CodeDevTask.status.in_(list(from_statuses)))
        .update(values, synchronize_session=False)
    )
    db.session.commit()
    return bool(updated)


def mark_queued(task_id: str) -> bool:
    """pending -> queued (the scheduler claimed the task)."""
    return _transition(task_id, {DevTaskStatus.PENDING}, DevTaskStatus.QUEUED)


def mark_in_progress(task_id: str, run_id: str) -> bool:
    """queued -> in_progress once the turn run is actually executing.

    Also accepts pending so a manually-started task-turn (no sprint) works.
    """
    return _transition(
        task_id,
        {DevTaskStatus.QUEUED, DevTaskStatus.PENDING},
        DevTaskStatus.IN_PROGRESS,
        last_attempt_run_id=run_id,
    )


def mark_verifying(task_id: str) -> bool:
    """in_progress -> verifying (edit finished, acceptance checks running)."""
    return _transition(task_id, {DevTaskStatus.IN_PROGRESS}, DevTaskStatus.VERIFYING)


def mark_cancelled(task_id: str) -> bool:
    """Any ACTIVE state -> cancelled (sprint/turn cancelled by the user)."""
    return _transition(task_id, DevTaskStatus.ACTIVE, DevTaskStatus.CANCELLED)


def mark_failed(task_id: str, reason: str) -> bool:
    """ACTIVE -> failed: infra-level failure (container crash / run exception)."""
    return _transition(
        task_id, DevTaskStatus.ACTIVE, DevTaskStatus.FAILED,
        note=(reason or "")[:1000],
    )


def release_to_pending(task_id: str, note: str | None = None, count_retry: bool = True) -> bool:
    """ACTIVE -> pending (retryable failure); bumps retry_count when counting."""
    task = db.session.get(CodeDevTask, task_id)
    if not task:
        return False
    fields = {}
    if note:
        fields["note"] = note[:1000]
    if count_retry:
        fields["retry_count"] = task.effective_retry_count + 1
    return _transition(task_id, DevTaskStatus.ACTIVE, DevTaskStatus.PENDING, **fields)


def retire_superseded(task_id: str, child_ids: str = "") -> bool:
    """pending|blocked -> skipped: a coarse ledger-seed task fully replaced by finer
    sub-tasks (auto-decomposition). ``SKIPPED`` is ``SETTLED_OK`` + terminal, so the
    retired parent neither blocks sprint completion nor gets claimed again. Fires
    only from pending/blocked — never clobbers a task a turn currently owns
    (ACTIVE), and a blocked monolith is retired in favour of its winnable children."""
    note = ("已细拆为更小的可验收子任务,由其承接" +
            (f":{child_ids}" if child_ids else ""))
    return _transition(
        task_id, {DevTaskStatus.PENDING, DevTaskStatus.BLOCKED}, DevTaskStatus.SKIPPED,
        note=note[:1000], blocked_reason=None,
    )


def mark_blocked(task_id: str, reason: str, from_statuses=None) -> bool:
    return _transition(
        task_id,
        from_statuses or (DevTaskStatus.ACTIVE | {DevTaskStatus.PENDING}),
        DevTaskStatus.BLOCKED,
        blocked_reason=(reason or "")[:1000],
    )


# --- ready derivation / claim ---------------------------------------------------
def _dep_state(session_tasks: list[CodeDevTask]) -> tuple[set, dict]:
    """(done feature_ids, feature_id -> status) over a session's tasks."""
    done_ids: set[str] = set()
    status_by_fid: dict[str, str] = {}
    for t in session_tasks:
        if not t.feature_id:
            continue
        status_by_fid[t.feature_id] = t.status
        if t.status == DevTaskStatus.DONE:
            done_ids.add(t.feature_id)
    return done_ids, status_by_fid


def _dep_verdict(task: CodeDevTask, done_ids: set, status_by_fid: dict) -> tuple[bool, str]:
    """(ready, dead_reason). ready=True → all deps done. dead_reason non-empty →
    the dependency chain can NEVER be satisfied (missing / terminally-not-done).
    A dead dep wins over a merely-waiting dep, so the task blocks instead of
    waiting forever."""
    waiting = False
    for dep in task.get_depends_on():
        if dep in done_ids:
            continue
        dep_status = status_by_fid.get(dep)
        if dep_status is None:
            return False, f"依赖缺失:{dep} 不在任务列表中"
        if dep_status in (DevTaskStatus.TERMINAL - {DevTaskStatus.DONE}):
            return False, f"依赖不可满足:{dep} 已 {dep_status}"
        waiting = True  # dep still pending/active — keep scanning for a dead one
    return (not waiting), ""


def session_tasks(session_id: str) -> list[CodeDevTask]:
    return (
        CodeDevTask.query.filter_by(session_id=session_id)
        .order_by(CodeDevTask.order_index.asc(), CodeDevTask.created_at.asc())
        .all()
    )


def block_dead_dependency_tasks(session_id: str) -> list[str]:
    """Auto-block pending tasks whose dependency chain can never be satisfied.
    Returns the blocked task ids (idempotent — already-blocked rows don't refire)."""
    tasks = session_tasks(session_id)
    done_ids, status_by_fid = _dep_state(tasks)
    blocked: list[str] = []
    for t in tasks:
        if t.status != DevTaskStatus.PENDING:
            continue
        ready, dead = _dep_verdict(t, done_ids, status_by_fid)
        if not ready and dead:
            if mark_blocked(t.id, dead, from_statuses={DevTaskStatus.PENDING}):
                blocked.append(t.id)
    return blocked


def ready_tasks(session_id: str, session_lane: str = "frontend") -> list[CodeDevTask]:
    """Schedulable tasks: pending, lane-compatible, all deps done. Ordered
    priority DESC then order_index ASC (the authored order breaks ties)."""
    lanes = _LANES_FOR_SESSION.get(session_lane, {session_lane})
    tasks = session_tasks(session_id)
    done_ids, status_by_fid = _dep_state(tasks)
    out = [
        t for t in tasks
        if t.status == DevTaskStatus.PENDING
        and t.effective_lane in lanes
        and _dep_verdict(t, done_ids, status_by_fid)[0]
    ]
    # Asset tasks first at equal priority (components then import real files),
    # per the design's "asset 先行" rule — encoded as a sort key, not a hard phase.
    out.sort(key=lambda t: (-t.effective_priority, 0 if t.category == "asset" else 1, t.order_index))
    return out


def claim_next_task(session_id: str, session_lane: str = "frontend") -> CodeDevTask | None:
    """Claim ONE ready task (pending -> queued) atomically.

    The conditional UPDATE is the race guard (functionally what ``FOR UPDATE SKIP
    LOCKED`` gives on Postgres, but portable to the sqlite test/dev DB): losing a
    race on one candidate just falls through to the next.
    """
    for candidate in ready_tasks(session_id, session_lane):
        if mark_queued(candidate.id):
            db.session.expire_all()
            return db.session.get(CodeDevTask, candidate.id)
    return None


def reconcile_stale_tasks(session_id: str) -> list[str]:
    """Heal tasks stranded in an ACTIVE state by a dead turn (service restart /
    crashed run): retry them (within budget) or fail them. Returns healed ids."""
    from backend.models.agent import AgentRun, AgentRunStatus

    healed: list[str] = []
    for t in session_tasks(session_id):
        if t.status not in DevTaskStatus.ACTIVE:
            continue
        run = db.session.get(AgentRun, t.last_attempt_run_id) if t.last_attempt_run_id else None
        if run is not None and run.status in AgentRunStatus.ACTIVE:
            continue  # a live turn is still driving it — leave it alone
        if t.effective_retry_count < max_retries_of(t):
            if release_to_pending(t.id, note="上次尝试被中断,已重新排队", count_retry=True):
                healed.append(t.id)
        else:
            if mark_failed(t.id, "多次尝试被中断,已标记失败(可人工重试)"):
                healed.append(t.id)
    return healed


# --- acceptance features (per-task verification) --------------------------------
def _verify_category(task: CodeDevTask) -> str:
    return "non_functional" if task.category == "nonfunctional" else "functional"


def _task_fid(task: CodeDevTask) -> str:
    return task.feature_id or f"T-{task.id[:8]}"


def ac_feature_items(task: CodeDevTask) -> list[dict]:
    """Verification items for THIS task: one per acceptance criterion (stable ids
    ``<fid>.AC1``…), or a single title-level item when no criteria are given.
    Shape matches ``_verify_support.features_from_ledger`` items."""
    fid = _task_fid(task)
    criteria = task.get_acceptance_criteria()
    if not criteria:
        desc = task.title if not task.description else f"{task.title}：{task.description}"
        return [{
            "id": fid, "category": _verify_category(task),
            "description": str(desc)[:400], "passes": False, "note": "",
        }]
    return [
        {
            "id": f"{fid}.AC{i + 1}", "category": _verify_category(task),
            "description": str(c)[:400], "passes": False, "note": "",
        }
        for i, c in enumerate(criteria)
    ]


def ac_ids_for(task: CodeDevTask) -> set:
    return {f["id"] for f in ac_feature_items(task)}


def features_from_dev_tasks(
    session_id: str, focus_task: CodeDevTask | None = None, max_regression: int = 30
) -> list[dict]:
    """The verification feature list for a task-focused turn, from the DB board
    (NOT the ledger): the focus task's acceptance criteria (all starting
    ``passes=false``) + the already-done tasks as a regression set (starting
    ``passes=true`` so an absent review keeps them passing; the reviewer
    explicitly failing one is a regression signal).

    Not-yet-done sibling tasks are deliberately EXCLUDED — the turn is judged on
    its own task, not the whole backlog (the design's "每轮只喂一个小任务").
    """
    feats: list[dict] = []
    if focus_task is not None:
        feats.extend(ac_feature_items(focus_task))
    done = [
        t for t in session_tasks(session_id)
        if t.status == DevTaskStatus.DONE and (focus_task is None or t.id != focus_task.id)
    ]
    for t in done[:max_regression]:
        feats.append({
            "id": _task_fid(t), "category": _verify_category(t),
            "description": str(t.title)[:400], "passes": True,
            "note": "已完成任务(回归检查:如已被破坏请判 false 并给证据)",
        })
    return feats


def apply_verify_outcome(
    task: CodeDevTask, run_id: str, features: list[dict], verification_blocking: bool,
    summary: str = "",
) -> dict:
    """Fold one turn's verification onto the task state machine.

    done ⇔ every acceptance item passes AND no done-task regression AND nothing
    blocking (house rules / runtime / explicit reviewer blockers). Otherwise the
    task goes back to pending (retry budget left) or blocked. Returns
    ``{status, passed, failed_criteria, regressed, note}``.
    """
    ac_ids = ac_ids_for(task)
    ac_items = [f for f in features if f.get("id") in ac_ids]
    failed_ac = [f for f in ac_items if not f.get("passes")]
    regressed = [
        str(f.get("id")) for f in features
        if f.get("id") not in ac_ids and not f.get("passes")
    ]
    passed = not failed_ac and not regressed and not verification_blocking and bool(ac_items)

    if passed:
        note = f"验收通过({len(ac_items)}/{len(ac_items)} 项标准)。{summary}".strip()
        _transition(
            task.id,
            {DevTaskStatus.VERIFYING, DevTaskStatus.IN_PROGRESS, DevTaskStatus.QUEUED},
            DevTaskStatus.DONE,
            origin_turn_run_id=run_id, note=note[:1000], blocked_reason=None,
        )
        return {"status": DevTaskStatus.DONE, "passed": True,
                "failed_criteria": [], "regressed": [], "note": note}

    reasons: list[str] = []
    if failed_ac:
        reasons.append(
            "未通过的验收标准:" + "；".join(
                f"[{f['id']}] {f.get('note') or f.get('description', '')}"[:160]
                for f in failed_ac[:6]
            )
        )
    if not ac_items:
        reasons.append("验收清单为空或评审未运行,无法判定通过")
    if regressed:
        reasons.append("回归:已完成任务被破坏 " + ", ".join(regressed[:5]))
    if verification_blocking:
        reasons.append(f"存在阻断问题:{summary}" if summary else "存在阻断问题(房规/运行时错误)")
    note = "；".join(reasons)[:1000]

    if task.effective_retry_count < max_retries_of(task):
        _transition(
            task.id,
            {DevTaskStatus.VERIFYING, DevTaskStatus.IN_PROGRESS, DevTaskStatus.QUEUED},
            DevTaskStatus.PENDING,
            retry_count=task.effective_retry_count + 1, note=note,
        )
        return {"status": DevTaskStatus.PENDING, "passed": False,
                "failed_criteria": [f["id"] for f in failed_ac],
                "regressed": regressed, "note": note}

    _transition(
        task.id,
        {DevTaskStatus.VERIFYING, DevTaskStatus.IN_PROGRESS, DevTaskStatus.QUEUED},
        DevTaskStatus.BLOCKED,
        blocked_reason=f"重试 {max_retries_of(task)} 次仍未通过:{note}"[:1000], note=note,
    )
    return {"status": DevTaskStatus.BLOCKED, "passed": False,
            "failed_criteria": [f["id"] for f in failed_ac],
            "regressed": regressed, "note": note}


# --- per-turn task brief --------------------------------------------------------
def build_task_brief(task: CodeDevTask, done_titles: dict | None = None) -> str:
    """The 任务喂入 brief for one turn — ONLY the current task + what it needs,
    never the whole backlog (the agent must not wander off-task)."""
    lines: list[str] = [
        "# 本回合任务(只完成这一个任务;不要顺带实现其它待办任务)",
        f"任务 ID: {_task_fid(task)}",
        f"标题: {task.title}",
    ]
    if task.description:
        lines.append(f"说明: {task.description}")

    lines.append("")
    lines.append("# 验收标准(回合结束会逐条验收,未全部通过将被打回重试)")
    criteria = task.get_acceptance_criteria()
    if criteria:
        lines.extend(f"- {c}" for c in criteria)
    else:
        lines.append("- 标题与说明所述能力真实可用(禁止占位/TODO)")

    deps = task.get_depends_on()
    if deps:
        lines.append("")
        lines.append("# 依赖(以下任务已完成,直接在其成果上继续,不要重做)")
        titles = done_titles or {}
        lines.extend(f"- {d} 已完成" + (f":{titles[d]}" if titles.get(d) else "") for d in deps)

    if task.category == "asset":
        spec = task.get_resource_spec()
        outputs = spec.get("outputs") if isinstance(spec.get("outputs"), list) else []
        skill = spec.get("skill") or "image-assets"
        lines.append("")
        lines.append(
            f"# 资源生成任务\n本任务是资源生成任务:调用 {skill} 技能生成下述真实位图"
            "(禁止 SVG 占位/远程 URL),生成后用 ls 确认每个文件存在且非 0 字节:"
        )
        for out in outputs:
            if isinstance(out, dict) and out.get("path"):
                size = f" ({out['size']})" if out.get("size") else ""
                lines.append(f"- {out['path']}{size}")

    if task.effective_retry_count > 0 and task.note:
        lines.append("")
        lines.append("# 上次尝试未通过的原因(本回合定向修复,勿重写已通过部分)")
        lines.append(task.note)

    lines.append("")
    lines.append(
        "# 禁止事项\n- 不要重写/重构整个工程,只围绕本任务做增量改动\n"
        "- 不要破坏已完成任务的功能与现有构建\n- 不要引入远程资源(远程图片/字体/CDN)"
    )
    return "\n".join(lines)


# --- shared bulk write (tasks/bulk route + planner apply) ---------------------------
class BulkWriteRefused(ValueError):
    """A guarded bulk write was refused (active sprint / in-flight tasks)."""


def bulk_write_tasks(
    project_id: str,
    session_id: str,
    tasks: list[dict],
    *,
    replace: bool = False,
    plan_id: str | None = None,
    source: str | None = None,
) -> dict:
    """Write a normalized task list onto the board with the P0 protections.

    ``tasks`` items are already-normalized dicts (feature_id / parent_feature_id /
    lane / category / title / description / acceptance_criteria / depends_on /
    resource_spec / priority / max_retries, optional planner_meta). Upserts by
    ``feature_id``; DONE / in-flight rows are never clobbered (skipped). ``replace``
    swaps the whole board and is refused while a sprint is active or any task is in
    flight. Returns ``{inserted, updated, skipped}``. Used by both the ``tasks/bulk``
    route and the backlog-planner apply so the two paths can never drift.
    """
    from sqlalchemy import func

    from backend.models.code.fullstack import CodeDevSprint, DevSprintStatus, DevTaskSource

    if replace:
        active_sprint = (
            CodeDevSprint.query.filter_by(session_id=session_id)
            .filter(CodeDevSprint.status.in_(list(DevSprintStatus.ACTIVE)))
            .first()
        )
        if active_sprint:
            raise BulkWriteRefused("Sprint 进行中，不能整板覆盖任务")
        in_flight = (
            CodeDevTask.query.filter_by(session_id=session_id)
            .filter(CodeDevTask.status.in_(list(DevTaskStatus.ACTIVE)))
            .count()
        )
        if in_flight:
            raise BulkWriteRefused("存在执行中的任务，不能整板覆盖")
        CodeDevTask.query.filter_by(session_id=session_id).delete(synchronize_session=False)
        db.session.commit()

    existing = {
        t.feature_id: t
        for t in CodeDevTask.query.filter_by(session_id=session_id).all()
        if t.feature_id
    }
    order = db.session.query(func.max(CodeDevTask.order_index)).filter_by(
        session_id=session_id
    ).scalar() or 0
    inserted = updated = skipped = 0
    for nt in tasks:
        row = existing.get(nt.get("feature_id")) if nt.get("feature_id") else None
        if row is not None:
            # Never clobber a delivered or in-flight task from a bulk write.
            if row.status in DevTaskStatus.ACTIVE or row.status == DevTaskStatus.DONE:
                skipped += 1
                continue
            row.parent_feature_id = nt.get("parent_feature_id")
            row.lane = nt.get("lane")
            row.category = nt.get("category") or "functional"
            row.title = nt["title"]
            row.description = nt.get("description")
            row.set_acceptance_criteria(nt.get("acceptance_criteria") or [])
            row.set_depends_on(nt.get("depends_on") or [])
            row.set_resource_spec(nt.get("resource_spec") or {})
            row.priority = nt.get("priority")
            if nt.get("max_retries") is not None:
                row.max_retries = nt["max_retries"]
            if plan_id:
                row.plan_id = plan_id
            if nt.get("planner_meta"):
                row.set_planner_meta(nt["planner_meta"])
            updated += 1
            continue
        order += 1
        task = CodeDevTask(
            project_id=project_id,
            session_id=session_id,
            feature_id=nt.get("feature_id"),
            parent_feature_id=nt.get("parent_feature_id"),
            lane=nt.get("lane"),
            category=nt.get("category") or "functional",
            title=nt["title"],
            description=nt.get("description"),
            status=DevTaskStatus.PENDING,
            source=source or DevTaskSource.USER_ADDED,
            priority=nt.get("priority"),
            max_retries=nt.get("max_retries"),
            order_index=order,
            plan_id=plan_id,
        )
        task.set_acceptance_criteria(nt.get("acceptance_criteria") or [])
        task.set_depends_on(nt.get("depends_on") or [])
        task.set_resource_spec(nt.get("resource_spec") or {})
        if nt.get("planner_meta"):
            task.set_planner_meta(nt["planner_meta"])
        db.session.add(task)
        inserted += 1
    db.session.commit()
    return {"inserted": inserted, "updated": updated, "skipped": skipped}


# --- progress snapshot ------------------------------------------------------------
def progress_snapshot(session_id: str, session_lane: str = "frontend") -> dict:
    """Status counts + readiness over the session's backlog (the sprint's pulse)."""
    tasks = session_tasks(session_id)
    counts: dict[str, int] = {}
    for t in tasks:
        counts[t.status] = counts.get(t.status, 0) + 1
    ready = ready_tasks(session_id, session_lane)
    unsettled = [t for t in tasks if t.status not in DevTaskStatus.TERMINAL]
    return {
        "total": len(tasks),
        "counts": counts,
        "ready": len(ready),
        "unsettled": len(unsettled),
        "done": counts.get(DevTaskStatus.DONE, 0),
        "settled_ok": sum(1 for t in tasks if t.status in DevTaskStatus.SETTLED_OK),
    }
