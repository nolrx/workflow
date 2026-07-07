"""
Dev Mode turn workflow (``code_dev_turn``).

One interactive development turn = one bounded ``AgentRun``:
  1. dev_prepare — load the project + dev session, ensure the long-running dev
     container is up (start it on the first turn), reload/merge the session-scoped
     consensus ledger, reconcile the persistent checklist from the ledger.
  2. dev_edit   — run ONE headless ``claude -p`` edit-mode round inside the dev
     container (Vite HMR then hot-reloads the right-pane preview). Skipped when the
     turn carries no instruction (a pure bootstrap/ensure-container turn).
  3. dev_verify — collect the workspace source, run the deterministic house-rules
     linter + the skeptical acceptance review, fold the results onto the checklist
     (atomic writes), emit CHECKLIST_UPDATED, and — if blocking defects remain and
     a repair budget is left — run one edit-mode repair round.

The long-running container lifecycle lives in ``dev_service`` and is DECOUPLED from
this run: the container survives the run finishing so the next turn reuses it. This
keeps "session = persistent container + checklist" separate from "turn = bounded
run", so the whole recorder / SSE / cancel / billing machinery is reused unchanged.

Design-review must-fix items honored here: the ledger reload order is
session-first (never clobbered by the full-generation ledger), the checklist is a
persistent per-session board written atomically, and requirement-changing turns
fold the user's instruction into the ledger as a high-priority decision.
Comments in English to match the Code/core convention.
"""
import io
import json
import logging
import os
import zipfile
from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy import func

from backend.extensions import db
from backend.models.agent import (
    AgentArtifact,
    AgentArtifactType,
    AgentEventLevel,
    AgentEventType,
    AgentRun,
    AgentRunStatus,
)
from backend.models.code import CodeProject
from backend.models.code.fullstack import (
    CodeDevSession,
    CodeDevTask,
    DevSessionStatus,
    DevTaskSource,
    DevTaskStatus,
)
from backend.services import pricing
from backend.services.agent.context_ledger import ContextLedger, seed_from_inputs
from backend.services.agent.workflows import _verify_support
from backend.services.code import (
    asset_lane,
    dev_backlog_planner_service,
    dev_sprint_service,
    house_rules,
)
from backend.services.code.dev_service import get_dev_service
from backend.services.code.frontend_project_service import get_frontend_project_service
from backend.services.code.template_service import TemplateSelection, get_code_template_service
from backend.services.credit_service import charge, refund_credits

logger = logging.getLogger(__name__)

# Larger ledger render budget for dev turns (long sessions accumulate decisions;
# a bigger window keeps early framework/tech-stack decisions in the prompt).
_LEDGER_RENDER_CHARS = int(os.getenv("DEV_MODE_LEDGER_CHARS", "4000"))
# One optional edit-mode repair round when the turn leaves blocking defects.
_DEV_REPAIR = os.getenv("CODE_DEV_TURN_REPAIR", "1") not in ("0", "false", "False", "")
_MAX_DOC_CHARS = 1500
_MAX_DIGEST_CHARS = 12_000

# Files whose change requires a dev-server RESTART (a dev server reads these at
# startup, not per-request; dependency changes also need npm install). Source edits
# DON'T — those are handled live by HMR. Kept framework-AGNOSTIC (not just Vite) so
# the rule is a universal norm for any web product the platform builds: dependency
# manifests + lockfiles, every common build/dev-server config, CSS pipeline config,
# TS/JS config, and env files (a restart is cheap + idempotent, so being inclusive is
# safe — better a needless ~15s restart than a stale config silently 500-ing).
_RESTART_TRIGGER_BASENAMES = frozenset({
    # dependency manifests + lockfiles (any package manager) — need npm/pnpm/yarn install
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb",
    # build / dev-server config (Vite, Vue, Svelte, Astro, Nuxt, Next, Remix, Webpack, Parcel, Rollup)
    "vite.config.ts", "vite.config.js", "vite.config.mjs", "vite.config.cjs",
    "vue.config.js", "svelte.config.js", "svelte.config.mjs",
    "astro.config.mjs", "astro.config.ts", "nuxt.config.ts", "nuxt.config.js",
    "next.config.js", "next.config.mjs", "next.config.ts", "remix.config.js",
    "webpack.config.js", "rollup.config.js", "rollup.config.mjs",
    # CSS pipeline (loaded at dev-server start)
    "tailwind.config.js", "tailwind.config.ts", "tailwind.config.cjs",
    "postcss.config.js", "postcss.config.cjs", "postcss.config.mjs",
    # TS / JS config
    "tsconfig.json", "jsconfig.json",
})


def _is_restart_trigger(fpath: str) -> bool:
    base = (fpath or "").replace("\\", "/").rsplit("/", 1)[-1]
    # .env / .env.* are read once at dev-server startup → a change needs a restart.
    if base == ".env" or base.startswith(".env."):
        return True
    return base in _RESTART_TRIGGER_BASENAMES

# A minimal, buildable Vite + React + TS scaffold so the dev server ALWAYS has
# something to run (the iframe is never blank). Used only when a session starts
# with no prior frontend source; the first turn then fleshes it out per the docs.
_MINIMAL_SCAFFOLD: dict[str, bytes] = {
    "package.json": json.dumps({
        "name": "dev-app",
        "private": True,
        "version": "0.0.0",
        "type": "module",
        "scripts": {"dev": "vite", "build": "vite build", "preview": "vite preview"},
        "dependencies": {"react": "^18.3.1", "react-dom": "^18.3.1"},
        "devDependencies": {
            "@vitejs/plugin-react": "^4.3.1",
            "typescript": "^5.5.3",
            "vite": "^5.4.0",
            "@types/react": "^18.3.3",
            "@types/react-dom": "^18.3.0",
        },
    }, indent=2).encode("utf-8"),
    "vite.config.ts": (
        "import { defineConfig } from 'vite'\n"
        "import react from '@vitejs/plugin-react'\n\n"
        "export default defineConfig({ plugins: [react()] })\n"
    ).encode("utf-8"),
    "tsconfig.json": json.dumps({
        "compilerOptions": {
            "target": "ES2020", "useDefineForClassFields": True, "lib": ["ES2020", "DOM", "DOM.Iterable"],
            "module": "ESNext", "skipLibCheck": True, "moduleResolution": "bundler",
            "allowImportingTsExtensions": True, "resolveJsonModule": True, "isolatedModules": True,
            "noEmit": True, "jsx": "react-jsx", "strict": True,
        },
        "include": ["src"],
    }, indent=2).encode("utf-8"),
    "index.html": (
        "<!doctype html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"UTF-8\" />\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />\n"
        "<title>Dev App</title>\n</head>\n<body>\n<div id=\"root\"></div>\n"
        "<script type=\"module\" src=\"/src/main.tsx\"></script>\n</body>\n</html>\n"
    ).encode("utf-8"),
    "src/main.tsx": (
        "import React from 'react'\n"
        "import ReactDOM from 'react-dom/client'\n"
        "import App from './App'\n\n"
        "ReactDOM.createRoot(document.getElementById('root')!).render(\n"
        "  <React.StrictMode>\n    <App />\n  </React.StrictMode>,\n)\n"
    ).encode("utf-8"),
    "src/App.tsx": (
        "export default function App() {\n"
        "  return (\n"
        "    <div style={{ fontFamily: 'system-ui', padding: 48 }}>\n"
        "      <h1>项目初始化中…</h1>\n"
        "      <p>开发模式已就绪,请在左侧对话框描述你的需求,开始搭建。</p>\n"
        "    </div>\n"
        "  )\n"
        "}\n"
    ).encode("utf-8"),
}


@dataclass
class DevSourceResolution:
    files: dict[str, bytes]
    kind: str
    template_selection: TemplateSelection | None = None


# --- checklist helpers (persistent per-session board) ------------------------
def seed_checklist(session_id: str, project_id: str, ledger_dict: dict) -> int:
    """Seed the persistent checklist from the ledger's FR/NFR (idempotent).

    Only inserts items whose ``feature_id`` isn't already on the board, so calling
    it again after a requirement change adds the new features without dupes.
    Returns the number of items inserted.
    """
    features = _verify_support.features_from_ledger(ledger_dict)
    existing = {
        t.feature_id
        for t in CodeDevTask.query.filter_by(session_id=session_id).all()
        if t.feature_id
    }
    order = db.session.query(func.max(CodeDevTask.order_index)).filter_by(
        session_id=session_id
    ).scalar() or 0
    inserted = 0
    for f in features:
        fid = f.get("id")
        if not fid or fid in existing:
            continue
        order += 1
        db.session.add(CodeDevTask(
            project_id=project_id,
            session_id=session_id,
            feature_id=str(fid),
            category="nonfunctional" if f.get("category") == "non_functional" else "functional",
            title=(f.get("description") or fid)[:300],
            description=f.get("description"),
            status=DevTaskStatus.PENDING,
            source=DevTaskSource.LEDGER_SEED,
            order_index=order,
        ))
        inserted += 1
    if inserted:
        db.session.commit()
    return inserted


def sync_checklist(session_id: str, project_id: str, features: list[dict], run_id: str) -> dict:
    """Fold verified feature results onto the persistent board with ATOMIC writes.

    ``features`` is the post-``apply_feature_results`` list (each with ``passes``).
    A passing feature flips its task to ``done`` via ``UPDATE ... WHERE status != done``
    (no read-then-write race across concurrent turns). Features not yet on the board
    (surfaced by a requirement change / the agent) are inserted as new tasks.
    Returns the current board dict.
    """
    existing = {
        t.feature_id: t
        for t in CodeDevTask.query.filter_by(session_id=session_id).all()
        if t.feature_id
    }
    order = db.session.query(func.max(CodeDevTask.order_index)).filter_by(
        session_id=session_id
    ).scalar() or 0
    changed = False
    for f in features:
        fid = str(f.get("id") or "")
        if not fid:
            continue
        task = existing.get(fid)
        if task is None:
            order += 1
            db.session.add(CodeDevTask(
                project_id=project_id, session_id=session_id, feature_id=fid,
                category="nonfunctional" if f.get("category") == "non_functional" else "functional",
                title=(f.get("description") or fid)[:300], description=f.get("description"),
                status=DevTaskStatus.DONE if f.get("passes") else DevTaskStatus.PENDING,
                source=DevTaskSource.AGENT_DISCOVERED, origin_turn_run_id=run_id, order_index=order,
                note=(f.get("note") or "")[:1000] if f.get("passes") else None,
            ))
            changed = True
            continue
        if f.get("passes") and task.status not in (DevTaskStatus.DONE, DevTaskStatus.SKIPPED):
            # Atomic flip: only rows still not-done are advanced. A SKIPPED row is a
            # deliberately-retired coarse parent (auto-decomposition) — its children
            # carry the work now, so an audit must not resurrect it to DONE.
            updated = db.session.query(CodeDevTask).filter(
                CodeDevTask.id == task.id,
                CodeDevTask.status.notin_((DevTaskStatus.DONE, DevTaskStatus.SKIPPED)),
            ).update(
                {
                    CodeDevTask.status: DevTaskStatus.DONE,
                    CodeDevTask.origin_turn_run_id: run_id,
                    CodeDevTask.note: (f.get("note") or "")[:1000],
                },
                synchronize_session=False,
            )
            if updated:
                changed = True
    if changed:
        db.session.commit()
    return checklist_board(session_id)


def checklist_board(session_id: str) -> dict:
    """The current checklist board (tasks ordered) + summary counts."""
    tasks = (
        CodeDevTask.query.filter_by(session_id=session_id)
        .order_by(CodeDevTask.order_index.asc(), CodeDevTask.created_at.asc())
        .all()
    )
    items = [t.to_dict() for t in tasks]
    done = sum(1 for t in tasks if t.status == DevTaskStatus.DONE)
    functional = [t for t in tasks if t.category == "functional"]
    functional_done = sum(1 for t in functional if t.status == DevTaskStatus.DONE)
    return {
        "items": items,
        "total": len(items),
        "done": done,
        "functional_total": len(functional),
        "functional_done": functional_done,
    }


# --- asset-task helpers (P2) ---------------------------------------------------
def _asset_outputs_of(task) -> list[dict]:
    """The task's normalized resource outputs; non-empty ⇔ asset verification applies."""
    if task is None:
        return []
    outputs, _ = asset_lane.normalize_outputs(task.get_resource_spec())
    return outputs


def _is_asset_task(task) -> bool:
    return task is not None and (task.category == "asset" or bool(_asset_outputs_of(task)))


def _asset_context_text(project, ledger: ContextLedger, task, outputs: list[dict]) -> str:
    """The style context gen-assets prepends to every image prompt — the project's
    visual baseline + this task's spec, so all imagery shares one style family."""
    spec = task.get_resource_spec()
    lines = [
        "# 项目视觉风格",
        (project.style_prompt or "")[:2500] or "(未提供,保持克制专业的现代风格)",
        "",
        "# 共识账本(节选)",
        ledger.render_for_prompt(max_chars=1200),
        "",
        "# 当前资源任务",
        f"任务 ID: {task.feature_id or task.id[:8]}",
        f"标题: {task.title}",
    ]
    if task.description:
        lines.append(f"说明: {task.description[:400]}")
    if spec.get("style_brief"):
        lines.append(f"style_brief: {str(spec['style_brief'])[:400]}")
    lines.append("outputs:")
    lines.extend(
        f"- {o['path']} {o.get('size', '')}: {o.get('prompt', '')}".rstrip(": ")
        for o in outputs
    )
    return "\n".join(lines)


def _fold_asset_validation(feats: list[dict], task, validation: dict) -> list[dict]:
    """Deterministically overrule the reviewer on the asset task's own AC items:
    required files verified present -> pass; missing/unprobed -> fail. The model's
    self-report never decides an asset task."""
    ac_ids = dev_sprint_service.ac_ids_for(task)
    ok = bool(validation.get("ok"))
    note = (
        validation.get("reason")
        or "产物已实测存在且非 0 字节:"
        + ", ".join(r["path"] for r in validation.get("outputs") or [] if r.get("exists"))[:300]
    )
    out = []
    for f in feats:
        nf = dict(f)
        if nf.get("id") in ac_ids:
            nf["passes"] = ok
            nf["note"] = note[:400]
        out.append(nf)
    return out


# --- ledger / source helpers -------------------------------------------------
def load_dev_ledger(session: CodeDevSession, project: CodeProject) -> ContextLedger:
    """Reload the consensus ledger session-first (must-fix #4): the session's own
    accumulated ledger wins, then the latest dev-turn run, then the full-generation
    run, then a fresh seed — so multi-turn steering is never clobbered."""
    sess_ledger = session.get_shared_ledger()
    if sess_ledger:
        led = ContextLedger.load(sess_ledger)
        if not led.is_empty():
            return led
    prior_turn = (
        AgentRun.query.filter_by(resource_id=project.id, workflow="code_dev_turn")
        .filter(AgentRun.id != session.id)
        .order_by(AgentRun.created_at.desc())
        .first()
    )
    if prior_turn and prior_turn.get_context_ledger():
        led = ContextLedger.load(prior_turn.get_context_ledger())
        if not led.is_empty():
            return led
    full = (
        AgentRun.query.filter_by(resource_id=project.id, workflow="code_full_generation")
        .order_by(AgentRun.created_at.desc())
        .first()
    )
    led = ContextLedger.load(full.get_context_ledger() if full else None)
    if led.is_empty():
        led = seed_from_inputs(
            project.requirement_input, project.title, project.get_selected_style_ids()
        )
    return led


_SRC_CODE_EXTS = (".tsx", ".jsx", ".ts", ".js", ".vue", ".svelte")


def is_runnable_vite(src: dict) -> bool:
    """True only when the seeded source is a runnable Vite app.

    A prior 'frontend' artifact isn't always a real Vite project — a scaffold-only /
    degraded run can leave just package.json + docs (no ``src/``, no vite dep), which
    would crash-loop ``npm run dev``. In that case the caller falls back to the known-
    good minimal scaffold so the dev server ALWAYS comes up (the "no code → scaffold"
    path). Runnable ⇔ a package.json that references vite (dep/devDep or a vite dev
    script) AND at least one source module."""
    if not src:
        return False
    pkg_key = min(
        (k for k in src if k.rsplit("/", 1)[-1] == "package.json" and "node_modules" not in k),
        key=lambda k: k.count("/"),
        default=None,
    )
    if not pkg_key:
        return False
    try:
        pkg = json.loads(bytes(src[pkg_key]).decode("utf-8", "replace"))
    except (ValueError, TypeError):
        return False
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    dev_script = str((pkg.get("scripts") or {}).get("dev") or "")
    has_vite = "vite" in deps or "vite" in dev_script
    has_source = any(
        "/src/" in ("/" + k) and k.endswith(_SRC_CODE_EXTS) for k in src
    ) or any(k.rsplit("/", 1)[-1] in ("main.tsx", "main.ts", "main.jsx", "main.js") for k in src)
    return bool(has_vite and has_source)


def _documents_digest(project: CodeProject) -> str:
    parts = []
    for document in project.documents.all():
        body = (document.content or "")[:_MAX_DOC_CHARS]
        parts.append(f"## {document.title} ({document.document_type})\n{body}")
    return "\n\n".join(parts)[:_MAX_DIGEST_CHARS]


def _template_context(project: CodeProject, documents_digest: str, contract_block: str) -> dict:
    return {
        "requirement": project.requirement_input or "",
        "requirements_doc": project.requirements_doc or "",
        "development_flow": project.development_flow or "",
        "documents_digest": documents_digest or "",
        "style_prompt": project.style_prompt or "",
        "contract_block": contract_block or "",
    }


def _resolve_source(project_id: str) -> dict:
    """The frontend source to seed the dev container with: the last built project when
    it's a runnable Vite app, else the minimal scaffold so the dev server ALWAYS runs
    (a scaffold-only / non-Vite prior artifact would otherwise crash-loop)."""
    from backend.services.agent.workflows._iteration_support import load_prior_source

    try:
        src = load_prior_source(project_id, "frontend")
    except Exception:  # noqa: BLE001
        src = {}
    if is_runnable_vite(src):
        return src
    return dict(_MINIMAL_SCAFFOLD)


def _resolve_source_for_project(
    project: CodeProject, contract_block: str = ""
) -> DevSourceResolution:
    """Resolve the dev-container seed for a project-aware bootstrap.

    Existing runnable source always wins. A brand-new frontend dev session then
    starts from the selected Code template when it is compatible with the Vite
    preview container; only template failures/non-runnable templates fall back to
    the tiny built-in scaffold.
    """
    from backend.services.agent.workflows._iteration_support import load_prior_source

    try:
        src = load_prior_source(project.id, "frontend")
    except Exception:  # noqa: BLE001
        src = {}
    if is_runnable_vite(src):
        return DevSourceResolution(files=src, kind="prior")

    try:
        selection = get_code_template_service().select(
            lane="frontend",
            **_template_context(project, _documents_digest(project), contract_block),
        )
    except Exception as exc:  # noqa: BLE001 - template selection must never block Dev Mode
        selection = TemplateSelection(
            lane="frontend",
            selected=False,
            warning=f"template selection failed: {str(exc)[:260]}",
        )

    if selection.selected:
        files = dict(selection.files or {})
        if is_runnable_vite(files):
            return DevSourceResolution(files=files, kind="template", template_selection=selection)
        selection = replace(
            selection,
            selected=False,
            files={},
            warning=(
                f"template {selection.template_path or selection.template_name or '(unknown)'} "
                "is not runnable by the current Vite dev preview; using built-in scaffold"
            ),
        )

    return DevSourceResolution(
        files=dict(_MINIMAL_SCAFFOLD),
        kind="minimal",
        template_selection=selection if selection.warning else None,
    )


def _emit_template_resolution(recorder, step_id: str, resolution: DevSourceResolution) -> None:
    selection = resolution.template_selection
    if not selection:
        return
    if resolution.kind == "template":
        recorder.emit(
            AgentEventType.PROGRESS,
            step_id=step_id,
            message=(
                f"已选择前端模板 {selection.template_path}，"
                f"开发容器将基于该脚手架启动（{len(selection.files)} 个文件）"
            ),
            payload=selection.event_payload(),
        )
        return
    if selection.warning:
        recorder.emit(
            AgentEventType.WARNING,
            level=AgentEventLevel.WARNING,
            step_id=step_id,
            message=f"前端模板不可用，改用内置开发骨架：{selection.warning}",
            payload=selection.event_payload(),
        )


def _load_contract_block(project_id: str) -> str:
    try:
        from backend.services.code.fullstack import contract_service

        row = contract_service.get_ledger(project_id)
        if row and row.contract_status == "ready":
            return contract_service.render_contract_for_prompt(
                row.get_api_contract(), include_db_schema=False
            )
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _source_digest(files: dict) -> str:
    """A compact rendering of the workspace source for the acceptance review.

    Leads with a FULL manifest of every text source file, THEN inlines contents up
    to a budget. The manifest matters: it tells the reviewer a file EXISTS even when
    its body was omitted for budget — so "not seen in the digest" is never mistaken
    for "not implemented" (that false inference produced whole-app FAIL verdicts that
    blocked every dev task). Over-budget files are truncated to a head + listed as
    omitted (never silently dropped), and smaller files keep getting inlined."""
    if not files:
        return ""
    texts: dict[str, str] = {}
    for rel in sorted(files):
        data = files[rel]
        if not isinstance(data, (bytes, bytearray)):
            continue
        try:
            text = bytes(data).decode("utf-8")
        except UnicodeDecodeError:
            continue  # binary asset — skip from the text digest
        if rel.split("/")[-1] in ("package-lock.json",) or rel.endswith(".map"):
            continue
        texts[rel] = text
    if not texts:
        return ""
    manifest = "\n".join(f"- {rel}（{len(t)} 字符）" for rel, t in texts.items())
    parts: list[str] = [
        "# 工程源码文件清单（以下文件均真实存在于工程中）\n"
        "# 重要：内容可能因体积预算未全部内联；凡下方标注「内容未内联/截断」的文件"
        "一律视为已存在,不得仅因其未出现在本摘要中就判功能缺失/未实现。\n" + manifest,
    ]
    budget = 90_000
    omitted: list[str] = []
    for rel, text in texts.items():
        chunk = f"\n// ===== {rel} =====\n{text}"
        if budget - len(chunk) < 0:
            head = text[:1200]
            parts.append(f"\n// ===== {rel}（内容截断,仅前段;完整实现以工程内文件为准）=====\n{head}")
            omitted.append(rel)
            budget -= len(head)
            continue
        parts.append(chunk)
        budget -= len(chunk)
    if omitted:
        parts.append(
            "\n// 注：以下文件的完整内容因预算未内联(但文件确实存在,请勿据此判缺失)："
            + "、".join(omitted)
        )
    return "".join(parts)


def _zip_source(files: dict) -> bytes:
    """Zip a ``{relpath: bytes}`` source tree (binary-safe)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for rel, content in (files or {}).items():
            if not rel:
                continue
            data = bytes(content) if isinstance(content, (bytes, bytearray)) else str(content).encode("utf-8")
            archive.writestr(rel, data)
    return buffer.getvalue()


def persist_source_snapshot(step, project_id: str, files: dict) -> None:
    """Persist the dev container's current source as a ``code_frontend_project_zip``
    artifact so a LATER session restores the WORK.

    The dev container fs (``/work``) is destroyed on stop (``docker rm``), so without
    this durable snapshot a re-entry falls back to the stale PRE-dev-mode source (or
    the minimal scaffold) via ``load_prior_source`` and appears to "rewrite the project
    from scratch". Writing the newest ``code_frontend_project_zip`` here means
    ``_resolve_source`` (which loads the newest) restores exactly what the user built.
    Best-effort — never fails a turn."""
    if not files:
        return
    try:
        step.add_artifact(
            AgentArtifactType.TEXT, "开发模式源码快照（zip）",
            filename="dev_snapshot.zip", mime_type="application/zip",
            write_file=True, content_bytes=_zip_source(files),
            domain_ref_type="code_frontend_project_zip", domain_ref_id=project_id,
        )
    except Exception:  # noqa: BLE001 — snapshot persistence must never sink a turn
        logger.warning("dev source snapshot persist failed for %s", project_id, exc_info=True)


def persist_snapshot_standalone(run_id: str, project_id: str, files: dict) -> bool:
    """Persist a dev source snapshot WITHOUT a recorder step (e.g. on session stop).

    Same durable ``code_frontend_project_zip`` artifact as ``persist_source_snapshot``,
    but callable outside a workflow (the stop endpoint has no step). Best-effort."""
    if not files or not run_id:
        return False
    try:
        from backend.services.agent.files import save_artifact_file

        rel = save_artifact_file(run_id, None, "dev_snapshot.zip", _zip_source(files))
        db.session.add(AgentArtifact(
            run_id=run_id, step_id=None, artifact_type=AgentArtifactType.TEXT,
            title="开发模式源码快照（zip）", filename="dev_snapshot.zip",
            mime_type="application/zip", storage_path=rel,
            domain_ref_type="code_frontend_project_zip", domain_ref_id=project_id,
        ))
        db.session.commit()
        return True
    except Exception:  # noqa: BLE001
        logger.warning("standalone dev snapshot persist failed for %s", project_id, exc_info=True)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False


def persist_dist_cache_standalone(run_id: str, project_id: str, dist_files: dict) -> bool:
    """Cache a WARM-built ``dist`` as a ``code_frontend_dist_zip`` artifact.

    Built inside the live dev container (node_modules warm) right before teardown so a
    later deploy can serve it directly — no cold ``npm install`` + build. Freshness is
    by ``created_at``: a deploy uses this cache only when it is at least as new as the
    newest source snapshot (else it cold-builds). Best-effort — never raises."""
    if not dist_files or not run_id:
        return False
    try:
        from backend.services.agent.files import save_artifact_file

        rel = save_artifact_file(run_id, None, "dev_dist.zip", _zip_source(dist_files))
        db.session.add(AgentArtifact(
            run_id=run_id, step_id=None, artifact_type=AgentArtifactType.TEXT,
            title="开发模式构建产物缓存（dist zip）", filename="dev_dist.zip",
            mime_type="application/zip", storage_path=rel,
            domain_ref_type="code_frontend_dist_zip", domain_ref_id=project_id,
        ))
        db.session.commit()
        return True
    except Exception:  # noqa: BLE001
        logger.warning("standalone dev dist cache persist failed for %s", project_id, exc_info=True)
        try:
            db.session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False


def _build_turn_prompt(
    project, injected, contract_block, instruction, features, is_bootstrap, template_hint: str = ""
):
    """Assemble the edit-mode instruction fed to the in-container claude.

    Grounds the agent in the project's documents + ledger + contract + the checklist
    (must-not-jump-the-framework), then states the user's ask for this turn.
    """
    feature_block = _verify_support.render_features_block(features)
    scaffold_note = (
        "本次为开发模式初始化:仅搭建项目框架/目录骨架/关键文件桩,并让 dev server 能正常启动;"
        "不需要完整实现所有功能(功能将在后续对话回合中逐步实现)。\n"
        if is_bootstrap else ""
    )
    template_note = (
        "本次开发容器已预置从 Code 模板仓库选择的前端脚手架。请先阅读并复用现有结构"
        "(package.json、vite 配置、src 目录、路由/组件/API client),在此基础上补齐当前项目骨架;"
        "不要从空目录重建,不要删除模板已有的通用工程能力。\n"
        f"模板选择信息:\n{template_hint}\n"
        if is_bootstrap and template_hint else ""
    )
    return "\n\n".join(p for p in [
        "# 你在一个长运行的开发容器里,基于现有工程做增量修改(edit-mode)。"
        "Vite dev server 正在运行,你的改动会被热重载(HMR)实时预览。"
        "严格遵守下述既定文档与共识账本,不要跳出既定框架;除非用户明确提出新需求。",
        scaffold_note,
        template_note,
        f"# 需求文档(节选)\n{(project.requirements_doc or '')[:4000]}",
        f"# 开发流程(节选)\n{(project.development_flow or '')[:2000]}" if project.development_flow else "",
        f"# 风格文档(节选)\n{(project.style_prompt or '')[:2000]}" if project.style_prompt else "",
        f"# 共识账本(权威口径,按 FR/NFR 编号引用,不要改述)\n{injected}" if injected else "",
        contract_block or "",
        feature_block,
        f"# 本回合用户诉求(这是你这一轮要完成的具体改动)\n{instruction}" if instruction else "",
        "# 交付要求\n真实可用实现,禁止占位/TODO;保持已完成功能与构建不被破坏;"
        "遵守房规(HashRouter、禁 Tailwind、根挂载等);改完确保 dev server 无编译错误。",
    ] if p)


# --- workflow entry ----------------------------------------------------------
def run_code_dev_turn_workflow(ctx, recorder) -> dict:
    """Entry point for the ``code_dev_turn`` workflow (one interactive dev turn)."""
    dev = get_dev_service()
    service = get_frontend_project_service()
    cfg = ctx.config or {}
    session_id = cfg.get("session_id")
    instruction = (cfg.get("instruction") or "").strip()
    is_bootstrap = bool(cfg.get("bootstrap"))
    # Sprint-scheduled task turn: the instruction is the task brief and the verify
    # step judges THIS task's acceptance criteria (+ done-task regression) instead
    # of the whole ledger checklist. Absent task_id → legacy behaviour unchanged.
    task_id = cfg.get("task_id")
    focus_task = None
    # Audit mode: a bootstrap turn over EXISTING (runnable) project code — run the
    # acceptance review once against the current repo so the checklist reflects what
    # is ALREADY implemented (calibrated to reality) instead of all-pending. No edit,
    # no repair — a read-only status check of the existing code.
    is_audit = bool(cfg.get("audit"))
    total_steps = 3
    completed = 0

    def progress(current: str) -> None:
        run = db.session.get(AgentRun, ctx.run_id)
        if run:
            run.set_progress({
                "total_steps": total_steps, "completed_steps": completed,
                "failed_steps": 0, "current_step": current,
            })
            db.session.commit()

    def cancel_result(project_id) -> dict:
        recorder.emit(
            AgentEventType.WARNING, level=AgentEventLevel.WARNING,
            message="收到取消请求，已停止本回合开发",
        )
        # A cancelled task turn releases its claim terminally (design: any
        # non-terminal state -> cancelled on user cancel).
        if task_id:
            dev_sprint_service.mark_cancelled(task_id)
        return {"status": AgentRunStatus.CANCELLED, "resource_id": project_id}

    ledger = ContextLedger.empty()
    injected = ""
    contract_block = ""
    features: list[dict] = []
    seed_template_hint = ""

    # --- Step 1: prepare -----------------------------------------------------
    with recorder.step(
        "dev_prepare", "开发准备 Agent", "planner", 1,
        input_summary=(instruction[:200] or "初始化开发会话"),
    ) as step:
        if not ctx.resource_id:
            raise ValueError("缺少 resource_id：开发模式需要一个已有的 Code 项目")
        project = CodeProject.query.filter_by(id=ctx.resource_id, user_id=ctx.user_id).first()
        if not project:
            raise ValueError("项目不存在或无权访问")
        session = db.session.get(CodeDevSession, session_id) if session_id else None
        if not session or session.project_id != project.id or session.user_id != ctx.user_id:
            raise ValueError("开发会话不存在或无权访问")
        project_id = project.id

        if not dev.is_available():
            raise RuntimeError("开发模式不可用：未配置容器运行时或 Anthropic 凭证")

        # Reload ledger session-first; fold a requirement-changing instruction in.
        ledger = load_dev_ledger(session, project)
        if instruction and not is_bootstrap:
            ledger.record_user_revision("dev", instruction)
        run = db.session.get(AgentRun, ctx.run_id)
        run.set_context_ledger(ledger.to_dict())
        session.set_shared_ledger(ledger.to_dict())
        db.session.commit()

        # Reconcile the persistent checklist from the (possibly grown) ledger.
        seed_checklist(session.id, project_id, ledger.to_dict())
        if task_id:
            focus_task = db.session.get(CodeDevTask, task_id)
            if not focus_task or focus_task.session_id != session.id:
                raise ValueError("任务不存在或不属于本开发会话")
            # queued -> in_progress (also accepts pending for a manual task turn).
            dev_sprint_service.mark_in_progress(focus_task.id, ctx.run_id)
            db.session.expire_all()
            focus_task = db.session.get(CodeDevTask, task_id)
            # Verify THIS task on its OWN acceptance criteria only — NOT a whole-app
            # regression sweep. Re-reviewing every done task against a (60k-)truncated
            # source digest produced false "源码未提供/页面组件未提供" regressions +
            # whole-app FAIL verdicts that blocked EVERY close (verify->repair->never
            # done). Real regressions are caught by the objective runtime smoke, which
            # actually loads + drives the app, not by re-reading partial source.
            features = dev_sprint_service.ac_feature_items(focus_task)
        else:
            features = _verify_support.features_from_ledger(ledger.to_dict())

        # Ensure the long-running dev container is up (start on the first turn).
        status = dev.container_status(project_id)
        if not status.get("running"):
            session.status = DevSessionStatus.STARTING
            db.session.commit()
            recorder.emit(
                AgentEventType.PROGRESS, step_id=step.id,
                message="正在启动长运行开发容器(npm install + npm run dev)…",
            )
            contract_block = _load_contract_block(project_id)
            source_resolution = _resolve_source_for_project(project, contract_block)
            _emit_template_resolution(recorder, step.id, source_resolution)
            if source_resolution.kind == "template" and source_resolution.template_selection:
                seed_template_hint = source_resolution.template_selection.prompt_hint()
            ok, err, info = dev.start_container(project_id, source_resolution.files)
            if not ok:
                session.status = DevSessionStatus.FAILED
                session.error_message = err
                db.session.commit()
                raise RuntimeError(f"开发容器启动失败：{err}")
            session.container_name = info.get("container_name")
            session.internal_port = info.get("internal_port")
            session.workdir = info.get("workdir")
            session.preview_path = info.get("preview_path")
            session.base_source_run_id = None
            db.session.commit()

        session.status = DevSessionStatus.RUNNING
        session.health = "healthy" if dev.health_check(project_id) else "unknown"
        session.last_active_at = datetime.utcnow()
        db.session.commit()

        recorder.emit(
            AgentEventType.DEV_PREVIEW_READY, step_id=step.id,
            message="开发容器就绪，实时预览可用",
            payload={"url": session.preview_path, "container": session.container_name},
        )
        step.set_output(
            output_summary=f"开发会话就绪：{project.title}",
            reasoning_summary="载入会话共识账本(会话优先,不被 full-generation 覆盖)并核对功能清单。",
            self_check=f"容器运行中；功能清单 {len(features)} 项。",
        )
    completed = 1
    progress("edit")
    if ctx.is_cancelled():
        return cancel_result(project_id)

    # --- Step 2: edit --------------------------------------------------------
    edit_ok = True
    asset_task = _is_asset_task(focus_task)
    asset_charged = 0
    if instruction:
        with recorder.step(
            "dev_edit", "开发 Agent", "generator", 2,
            input_summary="容器内 claude 增量编辑(edit-mode)",
        ) as step:
            injected = ledger.render_for_prompt(max_chars=_LEDGER_RENDER_CHARS)
            step.model_provider = "claude-code-cli (docker exec)"
            db.session.commit()

            # P2 asset task: meter the required images up-front (env-priced, default
            # 0) and write the style context gen-assets anchors every image to.
            if asset_task:
                outputs = _asset_outputs_of(focus_task)
                required_count = sum(1 for o in outputs if o.get("required"))
                amount = pricing.CODE_DEV_ASSET_IMAGE * required_count
                if amount > 0 and not charge(
                    user_id=ctx.user_id, amount=amount, operation="code_dev_asset_image",
                    resource_type="agent_run", resource_id=ctx.run_id,
                    description=f"Dev asset generation: {required_count} images",
                    team_id=ctx.team_id,
                ):
                    dev_sprint_service.mark_blocked(focus_task.id, "图片资源生成积分不足")
                    recorder.emit(
                        AgentEventType.WARNING, level=AgentEventLevel.WARNING, step_id=step.id,
                        message="积分不足,无法生成图片资源;任务已阻塞",
                        payload={"task_id": focus_task.id},
                    )
                    return {
                        "status": AgentRunStatus.COMPLETED, "resource_id": project_id,
                        "task_outcome": {
                            "status": "blocked", "passed": False, "failed_criteria": [],
                            "regressed": [], "note": "图片资源生成积分不足",
                        },
                    }
                asset_charged = amount
                if dev.write_asset_context(
                    project_id, _asset_context_text(project, ledger, focus_task, outputs)
                ):
                    recorder.emit(
                        AgentEventType.PROGRESS, step_id=step.id,
                        message="资源风格上下文已写入容器(所有图片将共用同一视觉基线)",
                        payload={"outputs": [o["path"] for o in outputs]},
                    )
            try:
                if not contract_block:
                    contract_block = _load_contract_block(project_id)
            except Exception:  # noqa: BLE001
                contract_block = ""

            # Track whether the turn touches package.json / vite.config — those need a
            # dev-server RESTART (Vite only re-reads its config on restart; new/removed
            # deps need npm install). Source-only edits are handled live by Vite/HMR.
            config_touched = [False]

            def on_event(event: dict) -> None:
                etype = event.get("type")
                if etype == "assistant":
                    for block in event.get("message", {}).get("content", []):
                        if not isinstance(block, dict) or block.get("type") != "tool_use":
                            continue
                        name = block.get("name") or ""
                        inp = block.get("input") or {}
                        if name in ("Write", "Edit"):
                            fpath = inp.get("file_path") or inp.get("path") or ""
                            if _is_restart_trigger(fpath):
                                config_touched[0] = True
                            recorder.emit(
                                AgentEventType.FILE_CREATED, step_id=step.id,
                                message=f"写入 {fpath}",
                                payload={"tool": name, "file": fpath},
                            )
                        else:
                            cmd = inp.get("command") or ""
                            recorder.emit(
                                AgentEventType.TOOL_CALL, step_id=step.id,
                                message=(f"{name}: {cmd[:80]}" if cmd else name),
                                payload={"tool": name, "command": cmd[:500]},
                            )
                elif etype == "user":
                    for block in event.get("message", {}).get("content", []):
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            content = block.get("content")
                            text = content if isinstance(content, str) else json.dumps(
                                content, ensure_ascii=False)
                            recorder.emit(
                                AgentEventType.TOOL_RESULT, step_id=step.id,
                                message="工具返回", payload={"output": (text or "")[:2000]},
                            )

            # Task turn: the edit prompt carries only THIS task's acceptance items
            # (the brief in `instruction` is the ask); the full set — including the
            # done-task regression list — is for the verify step's reviewer.
            prompt_features = (
                dev_sprint_service.ac_feature_items(focus_task) if focus_task else features
            )
            prompt = _build_turn_prompt(
                project, injected, contract_block, instruction, prompt_features, is_bootstrap,
                seed_template_hint,
            )
            res = dev.exec_turn(project_id, prompt, on_event=on_event, is_cancelled=ctx.is_cancelled)
            if res.cancelled:
                return cancel_result(project_id)
            edit_ok = res.success
            if not res.success:
                recorder.emit(
                    AgentEventType.WARNING, level=AgentEventLevel.WARNING, step_id=step.id,
                    message=f"本回合编辑未干净完成（{res.error or '未知'}），将基于当前工作区验证。",
                )
            # A change to package.json / vite.config needs a dev-server restart so Vite
            # reloads its config + installs new deps (otherwise a stale plugin 500s).
            if config_touched[0] and not ctx.is_cancelled():
                recorder.emit(
                    AgentEventType.PROGRESS, step_id=step.id,
                    message="检测到依赖/构建配置变更,正在重启开发服务器(装新依赖 + 重载配置)…",
                    payload={"restart": True},
                )
                if dev.restart_dev_server(project_id):
                    recorder.emit(
                        AgentEventType.DEV_PREVIEW_READY, step_id=step.id,
                        message="开发服务器已重启,预览已刷新", payload={"restarted": True},
                    )
                else:
                    recorder.emit(
                        AgentEventType.WARNING, level=AgentEventLevel.WARNING, step_id=step.id,
                        message="开发服务器重启后未就绪,请稍后手动刷新预览",
                    )
            step.set_output(
                output_summary="已在容器内完成一轮增量编辑（HMR 已热更预览）。" if edit_ok
                else "编辑未完全成功，已尽力应用改动。",
            )
    else:
        # Pure bootstrap/ensure turn with no instruction: container is up, checklist
        # seeded — nothing to edit this turn.
        recorder.emit(AgentEventType.PROGRESS, message="开发容器已就绪，等待你的第一条指令。")
    completed = 2
    progress("verify")
    if ctx.is_cancelled():
        return cancel_result(project_id)

    # --- Step 3: verify + checklist ------------------------------------------
    with recorder.step(
        "dev_verify", "验证与验收 Agent", "reviewer", 3,
        input_summary="房规检查 + 验收评审 + 更新功能清单",
    ) as step:
        if focus_task is not None:
            dev_sprint_service.mark_verifying(focus_task.id)
        files = dev.collect_source(project_id)
        violations = house_rules.check_frontend(files) if files else []
        review = None
        # Run the acceptance review when there's an instruction (normal turn) OR in
        # audit mode (bootstrap over existing code) — the latter calibrates the
        # checklist to what the current repo already implements.
        if files and (instruction or is_audit):
            try:
                review = service.review_project(
                    source_digest=_source_digest(files),
                    requirements_registry=(injected or ledger.render_for_prompt(max_chars=_LEDGER_RENDER_CHARS)),
                    style_prompt=project.style_prompt or "",
                    features_block=_verify_support.render_features_block(features),
                    house_rules_report=house_rules.render_report(violations),
                    on_model_call=step.model_tracer() if hasattr(step, "model_tracer") else None,
                )
            except Exception as exc:  # noqa: BLE001 — advisory, never fatal
                logger.warning("dev verify review raised: %s", exc)
                review = None

        feats, stats = _verify_support.apply_feature_results(
            features, (review or {}).get("feature_results")
        )
        # P2 asset task: verify the outputs INSIDE the container (existence +
        # non-zero size) and overrule the reviewer's opinion of this task's AC —
        # a file either exists or it doesn't, the model doesn't get a vote.
        asset_validation = None
        if asset_task:
            asset_validation = dev.validate_resource_outputs(
                project_id, focus_task.get_resource_spec()
            )
            feats = _fold_asset_validation(feats, focus_task, asset_validation)
            stats = {"total": len(feats), "passed": sum(1 for f in feats if f.get("passes")),
                     "failed": sum(1 for f in feats if not f.get("passes"))}
        verification = _verify_support.Verification(
            house_rule_errors=house_rules.errors(violations),
            house_rule_warnings=house_rules.warnings(violations),
            review=review, features=feats, feature_stats=stats,
        )

        # One optional edit-mode repair round (bounded). A task turn also repairs
        # when its own acceptance criteria failed — cheaper than burning a whole
        # retry turn on a defect the reviewer just pinpointed. An asset task whose
        # lane is DEAD (no codex/key) is not repairable in-turn — skip straight to
        # the blocked path instead of burning a claude round.
        def _needs_repair(v: _verify_support.Verification, feat_list: list) -> bool:
            if asset_validation is not None and asset_validation.get("blocking"):
                return False
            if focus_task is None:
                # Non-task turn (bootstrap / manual / audit): keep the whole-app gate.
                return v.blocking
            # Task turn: repair only on the task's OWN failed AC or an OBJECTIVE hard
            # blocker (house-rule / runtime / score) — never on the reviewer's
            # subjective whole-app blocking_issues (poisoned by a truncated digest).
            if v.objective_blocking:
                return True
            ac_ids = dev_sprint_service.ac_ids_for(focus_task)
            return any(f.get("id") in ac_ids and not f.get("passes") for f in feat_list)

        if _DEV_REPAIR and _needs_repair(verification, feats) and instruction and not ctx.is_cancelled():
            recorder.emit(
                AgentEventType.WARNING, level=AgentEventLevel.WARNING, step_id=step.id,
                message=f"验证发现问题({verification.summary_line()})，启动一次定向修复。",
                payload={"blocking": verification.blocking, "feature_stats": stats},
            )
            repaired = dev.exec_turn(
                project_id,
                "# 定向修复(edit-mode,只修下述问题,勿重写已通过功能)\n"
                + verification.repair_instruction(),
                is_cancelled=ctx.is_cancelled,
            )
            if repaired.cancelled:
                return cancel_result(project_id)
            files = dev.collect_source(project_id) or files
            violations = house_rules.check_frontend(files) if files else violations
            if files and instruction:
                try:
                    review = service.review_project(
                        source_digest=_source_digest(files),
                        requirements_registry=(injected or ""),
                        style_prompt=project.style_prompt or "",
                        features_block=_verify_support.render_features_block(features),
                        house_rules_report=house_rules.render_report(violations),
                    ) or review
                except Exception:  # noqa: BLE001
                    pass
            feats, stats = _verify_support.apply_feature_results(features, (review or {}).get("feature_results"))
            # Asset task: re-probe the outputs after the repair round and overrule
            # the reviewer again (the repair may have (re)generated the files).
            if asset_task:
                asset_validation = dev.validate_resource_outputs(
                    project_id, focus_task.get_resource_spec()
                )
                feats = _fold_asset_validation(feats, focus_task, asset_validation)
                stats = {"total": len(feats),
                         "passed": sum(1 for f in feats if f.get("passes")),
                         "failed": sum(1 for f in feats if not f.get("passes"))}
            verification = _verify_support.Verification(
                house_rule_errors=house_rules.errors(violations),
                house_rule_warnings=house_rules.warnings(violations),
                review=review, features=feats, feature_stats=stats,
            )

        # Durable snapshot of the work — so stopping the session (which destroys the
        # container fs) and re-entering RESTORES this code instead of rewriting from
        # scratch. Only when there was an edit (a no-instruction audit/bootstrap re-uses
        # the source it was just seeded with, already the newest snapshot).
        if instruction:
            persist_source_snapshot(step, project_id, files)

        # Fold onto the persistent board (atomic) + broadcast the live update.
        task_outcome = None
        if focus_task is not None:
            if asset_validation is not None and asset_validation.get("blocking"):
                # Unfixable-by-retry asset defect (illegal paths / lane unavailable):
                # block the task directly instead of burning its retry budget, and
                # refund the per-image charge when the lane never even fired.
                reason = asset_validation.get("reason") or "资源生成环境不可用"
                dev_sprint_service.mark_blocked(focus_task.id, reason)
                if asset_charged > 0 and (
                    (asset_validation.get("diagnostics") or {}).get("calls", 0) == 0
                ):
                    refund_credits(
                        ctx.user_id, asset_charged, "code_dev_asset_image",
                        "agent_run", ctx.run_id,
                        description="refund dev asset images (lane unavailable)",
                        team_id=ctx.team_id,
                    )
                    asset_charged = 0
                recorder.emit(
                    AgentEventType.WARNING, level=AgentEventLevel.WARNING, step_id=step.id,
                    message=f"资源任务已阻塞:{reason}",
                    payload={"task_id": focus_task.id,
                             "diagnostics": asset_validation.get("diagnostics") or {}},
                )
                task_outcome = {
                    "status": DevTaskStatus.BLOCKED, "passed": False,
                    "failed_criteria": sorted(dev_sprint_service.ac_ids_for(focus_task)),
                    "regressed": [], "note": reason,
                }
            else:
                if asset_validation is not None and asset_validation.get("ok"):
                    # Evidence for the UI/audit: what was actually verified on disk.
                    spec = focus_task.get_resource_spec()
                    spec["verified_outputs"] = asset_validation.get("outputs") or []
                    focus_task.set_resource_spec(spec)
                    db.session.commit()
                # Task turn: drive the task state machine (done / retry / blocked)
                # instead of sync_checklist — the AC sub-items (FRx.Ty.ACn) must not
                # be inserted onto the board as new tasks. Closure gates on OBJECTIVE
                # blockers only (not the reviewer's whole-app FAIL); ``feats`` already
                # holds this task's AC only, so there is no cross-task regression here.
                task_outcome = dev_sprint_service.apply_verify_outcome(
                    focus_task, ctx.run_id, feats, verification.objective_blocking,
                    summary=verification.summary_line(),
                )
            db.session.expire_all()
            board = checklist_board(session.id)
            _progress = f"{board['functional_done']}/{board['functional_total']}"
            _fid = focus_task.feature_id or focus_task.id[:8]
            recorder.emit(
                AgentEventType.CHECKLIST_UPDATED, step_id=step.id,
                message=(
                    f"任务 [{_fid}] 验收通过；功能进度 {_progress}"
                    if task_outcome["passed"]
                    else f"任务 [{_fid}] 未通过（→ {task_outcome['status']}）；功能进度 {_progress}"
                ),
                payload={
                    "board": board, "feature_stats": stats,
                    "task_id": focus_task.id, "task_outcome": task_outcome,
                },
            )
        else:
            board = sync_checklist(session.id, project_id, feats, ctx.run_id)
            _progress = f"{board['functional_done']}/{board['functional_total']}"
            recorder.emit(
                AgentEventType.CHECKLIST_UPDATED, step_id=step.id,
                message=(
                    f"已按现有仓库代码校准功能清单:{_progress} 已实现"
                    if is_audit and not instruction else f"功能进度 {_progress}"
                ),
                payload={"board": board, "feature_stats": stats, "audit": bool(is_audit and not instruction)},
            )
            # Session-ready auto-decompose (bootstrap only). Now that the audit has
            # calibrated which FRs the existing code already delivers (DONE), split
            # the REMAINING coarse ledger-seed FRs — a whole FR with no granular AC,
            # still PENDING — into small tasks with concrete AC, so the DEFAULT board
            # the user sees is already fine-grained WITHOUT applying a plan or starting
            # a sprint. Runs AFTER the fold so already-delivered FRs (now DONE) are not
            # candidates and never get re-planned. Advisory: any failure leaves the
            # coarse tasks in place (no worse than before).
            if is_bootstrap:
                try:
                    dec = dev_backlog_planner_service.decompose_coarse_seed_tasks(
                        project, session, style_hint=project.style_prompt or "",
                    )
                except Exception:  # noqa: BLE001 — a split failure must not sink the turn
                    logger.warning("bootstrap auto-decompose raised", exc_info=True)
                    dec = None
                if dec and dec["decomposed"]:
                    board = checklist_board(session.id)
                    recorder.emit(
                        AgentEventType.CHECKLIST_UPDATED, step_id=step.id,
                        message=(
                            f"已把 {dec['decomposed']} 个粗需求细拆为 "
                            f"{dec['sub_tasks']} 个可单回合完成的子任务(带具体验收标准),"
                            "任务板已就绪。"
                        ),
                        payload={"board": board, "decomposed": dec},
                    )

        # Persist the merged ledger back to the session (session-scoped truth).
        session.set_shared_ledger(ledger.to_dict())
        session.last_active_at = datetime.utcnow()
        db.session.commit()

        step.add_artifact(
            "json", "本回合验证结果",
            content_json=verification.to_record(),
            domain_ref_type="code_dev_turn", domain_ref_id=session.id,
        )
        step.set_output(
            output_summary=f"验证:{verification.summary_line()}",
            self_check=f"功能清单 {board['functional_done']}/{board['functional_total']} 通过。",
        )
    completed = 3
    progress("done")
    result = {"status": AgentRunStatus.COMPLETED, "resource_id": project_id}
    if task_outcome is not None:
        result["task_outcome"] = task_outcome
    if asset_charged:
        # Per-image metering charged as the task started — fold into credit_used.
        result["extra_credits"] = asset_charged
    return result
