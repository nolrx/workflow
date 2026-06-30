"""
Code domain workflow — ``code_frontend_project_generation``.

The frontend code-generation path (the removed single-file ``code_frontend_generation``
is gone): an autonomous coding CLI runs in a sandboxed container and produces a
COMPLETE multi-file React + Vite + TypeScript project that it builds and
self-checks. The CLI's stream-json is translated live into AgentEvents
(``file_created`` / ``tool_call`` / ``tool_result``), so the existing timeline UI
replays every file write and build command.

Two agents cooperate in the container: Claude Code writes the project, and when
the UI needs real raster imagery it triggers the bundled ``image-assets`` skill,
which shells out to the OpenAI Codex CLI to generate pictures via the image model
(see ``frontend_project_service``). Those ``gen-assets`` / Codex invocations are
surfaced on the timeline as distinct asset-generation events.

Steps: ``fe_planner`` -> ``fe_project_build`` -> ``fe_publish`` (3 counted steps;
the agent's own build + self-check loop replaces the removed single-file path's
critic/repair, and an advisory FR/NFR acceptance review runs at the end of the
build step — a PASS/CONCERNS/FAIL verdict with per-FR coverage that is recorded
and surfaced but never blocks publish). The build runs in
``frontend_project_service`` which owns the container lifecycle; this workflow
only assembles context, maps events, runs the review, and publishes the
deliverable (source zip + previewable dist).
"""
import io
import json
import logging
import os
import zipfile

from backend.extensions import db
from backend.models.agent import (
    AgentArtifactType,
    AgentEventLevel,
    AgentEventType,
    AgentRun,
    AgentRunStatus,
)
from backend.models.code import CodeFigmaDesign, CodeProject, CodeProjectStatus
from backend.services import pricing
from backend.services.agent.context_ledger import ContextLedger, seed_from_inputs
from backend.services.agent.context_verifier import gate_available
from backend.services.agent.files import agent_run_dir
from backend.services.agent.workflows import _verify_support
from backend.services.code import house_rules, scaffold
from backend.services.code.figma import storage as figma_storage
from backend.services.code.frontend_project_service import get_frontend_project_service
from backend.services.credit_service import charge

logger = logging.getLogger(__name__)

TOTAL_STEPS = 3  # planner, build, publish
_MAX_DOC_CHARS = 1500
_MAX_DIGEST_CHARS = 12_000


def _documents_digest(project: CodeProject) -> str:
    parts = []
    for document in project.documents.all():
        body = (document.content or "")[:_MAX_DOC_CHARS]
        parts.append(f"## {document.title} ({document.document_type})\n{body}")
    return "\n\n".join(parts)[:_MAX_DIGEST_CHARS]


_CODE_EXTS = (".tsx", ".ts", ".jsx", ".js", ".css", ".html", ".json", ".md")
_REVIEW_MAX_FILE = 4000
_REVIEW_MAX_TOTAL = 60_000


def _source_digest(files: dict) -> str:
    """Concatenate the textual source files (skip binary assets) for acceptance review."""
    parts: list[str] = []
    total = 0
    for path in sorted(files):
        low = path.lower()
        if not low.endswith(_CODE_EXTS) or "node_modules/" in low or low.startswith("dist/"):
            continue
        raw = files[path]
        try:
            text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        except (UnicodeDecodeError, AttributeError):
            continue
        chunk = f"// ===== {path} =====\n{text[:_REVIEW_MAX_FILE]}"
        parts.append(chunk)
        total += len(chunk)
        if total >= _REVIEW_MAX_TOTAL:
            break
    return "\n\n".join(parts)[:_REVIEW_MAX_TOTAL]


def _load_figma_frames(project_id: str) -> list[dict]:
    """Frames of the project's attached Figma design for the build (name/ir/render).

    Returns ``[{name, ir_text, render_path}]`` (render_path = absolute path to the
    stored PNG), or ``[]`` when no design is attached.
    """
    design = CodeFigmaDesign.query.filter_by(project_id=project_id).first()
    if not design:
        return []
    frames: list[dict] = []
    for frame in design.get_frames():
        render_filename = frame.get("render_filename")
        frames.append(
            {
                "name": frame.get("name"),
                "ir_text": frame.get("ir_text") or "",
                "render_path": (
                    str(figma_storage.render_path(project_id, render_filename))
                    if render_filename
                    else None
                ),
            }
        )
    return frames


def run_code_frontend_project_workflow(ctx, recorder) -> dict:
    """Generate + build + publish a runnable multi-file frontend project."""
    service = get_frontend_project_service()
    completed = 0
    ledger = ContextLedger.empty()

    def progress(current_step: str) -> None:
        run = db.session.get(AgentRun, ctx.run_id)
        if run:
            run.set_progress(
                {
                    "total_steps": TOTAL_STEPS,
                    "completed_steps": completed,
                    "failed_steps": 0,
                    "current_step": current_step,
                }
            )
            db.session.commit()
        recorder.emit(
            AgentEventType.PROGRESS,
            message=f"进度 {completed}/{TOTAL_STEPS}",
            payload={"completed": completed, "total": TOTAL_STEPS, "current": current_step},
        )

    def cancel_result(project_id) -> dict:
        recorder.emit(
            AgentEventType.WARNING,
            level=AgentEventLevel.WARNING,
            message="收到取消请求，停止后续步骤",
        )
        return {"status": AgentRunStatus.CANCELLED, "resource_id": project_id}

    # --- Step 1: Planner -----------------------------------------------------
    with recorder.step(
        "fe_planner", "前端规划 Agent", "planner", 1, input_summary="校验已确认项目并准备构建上下文"
    ) as step:
        if not ctx.resource_id:
            raise ValueError("缺少 resource_id：前端项目生成需要一个已有的 Code 项目")
        project = CodeProject.query.filter_by(id=ctx.resource_id, user_id=ctx.user_id).first()
        if not project:
            raise ValueError("项目不存在或无权访问")
        if not project.requirements_doc or not project.style_prompt:
            raise ValueError("项目尚未完成需求文档与风格文档，无法生成前端")
        if project.status != CodeProjectStatus.UI_CONFIRMED:
            recorder.emit(
                AgentEventType.WARNING,
                level=AgentEventLevel.WARNING,
                step_id=step.id,
                message="项目尚未确认 UI 基调（ui_confirmed），将按当前内容生成前端",
            )
        project_id = project.id

        # Reuse the consensus ledger from the most recent full-generation run.
        prior = (
            AgentRun.query.filter_by(resource_id=project_id, workflow="code_full_generation")
            .order_by(AgentRun.created_at.desc())
            .first()
        )
        ledger = ContextLedger.load(prior.get_context_ledger() if prior else None)
        if ledger.is_empty():
            ledger = seed_from_inputs(
                project.requirement_input, project.title, project.get_selected_style_ids()
            )
        fe_run = db.session.get(AgentRun, ctx.run_id)
        fe_run.set_context_ledger(ledger.to_dict())
        db.session.commit()

        if not service.is_configured():
            recorder.emit(
                AgentEventType.WARNING,
                level=AgentEventLevel.WARNING,
                step_id=step.id,
                message="未配置 ANTHROPIC_API_KEY，无法运行容器化前端生成",
            )
        elif not service.assets_enabled():
            recorder.emit(
                AgentEventType.PROGRESS,
                step_id=step.id,
                message="未配置 OPENAI_API_KEY：将跳过图片资源生成（改用 CSS/SVG，不使用 emoji）",
                payload={"assets_enabled": False},
            )

        step.set_context(snapshot={"injected_text": "", "ledger": ledger.to_dict()})
        step.set_output(
            output_summary=f"已校验项目「{project.title}」，准备生成完整前端工程。",
            reasoning_summary="确认需求/风格齐备并汇总文档；载入上一轮共识账本，保证口径一致。",
            decision_notes="技术栈固定为 React + Vite + TypeScript 多文件工程，由沙箱内 agent 自行构建与自检。",
            self_check=f"文档 {project.documents.count()} 份；UI 基调确认：{project.status == CodeProjectStatus.UI_CONFIRMED}。",
            next_action="在沙箱容器内生成完整前端项目。",
        )
    completed += 1
    progress("build")

    # --- Step 2: Container build (agentic) -----------------------------------
    if ctx.is_cancelled():
        return cancel_result(project_id)
    result: dict = {}
    with recorder.step(
        "fe_project_build", "前端工程 Agent", "generator", 2,
        input_summary="在沙箱容器内自主生成 + 构建完整前端工程",
    ) as step:
        project = db.session.get(CodeProject, project_id)
        injected = ledger.render_for_prompt()
        step.model_provider = "claude-code-cli + codex-cli"
        db.session.commit()

        def on_event(event: dict) -> None:
            """Translate the CLI's stream-json into AgentEvents (live timeline)."""
            etype = event.get("type")
            if etype == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    name = block.get("name") or ""
                    inp = block.get("input") or {}
                    if name in ("Write", "Edit"):
                        fpath = inp.get("file_path") or inp.get("path") or ""
                        recorder.emit(
                            AgentEventType.FILE_CREATED, step_id=step.id,
                            message=f"写入 {fpath}",
                            payload={"tool": name, "file": fpath},
                        )
                    elif name == "Skill":
                        skill = inp.get("command") or inp.get("name") or inp.get("skill") or ""
                        recorder.emit(
                            AgentEventType.TOOL_CALL, step_id=step.id,
                            message=f"触发技能:{skill or 'image-assets'}",
                            payload={"tool": name, "skill": skill},
                        )
                    else:
                        cmd = inp.get("command") or ""
                        # Claude shells out to Codex (via the image-assets skill)
                        # for real raster assets — surface that distinctly.
                        is_assets = "gen-assets" in cmd or "codex" in cmd
                        recorder.emit(
                            AgentEventType.TOOL_CALL, step_id=step.id,
                            message=(
                                "调用 Codex 生成资源图片(image-assets 技能)"
                                if is_assets else (f"{name}: {cmd[:80]}" if cmd else name)
                            ),
                            payload={
                                "tool": name, "command": cmd[:500],
                                **({"lane": "codex-assets"} if is_assets else {}),
                            },
                        )
            elif etype == "user":
                for block in event.get("message", {}).get("content", []):
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    content = block.get("content")
                    text = content if isinstance(content, str) else json.dumps(
                        content, ensure_ascii=False
                    )
                    recorder.emit(
                        AgentEventType.TOOL_RESULT, step_id=step.id,
                        message="工具返回", payload={"output": (text or "")[:2000]},
                    )
            elif etype == "fe_phase":
                # Sentinel from the container's self-healing build ladder.
                phase = event.get("phase") or ""
                labels = {
                    "install": "安装依赖（npm install）",
                    "build": "运行构建（npm run build）",
                    "ai-repair": "构建未通过，启动 AI 定向修复",
                    "stub": "构建仍未通过，确定性补桩缺失模块",
                    "vite-only": "跳过类型检查，直接用 Vite 构建",
                    "fallback": "构建仍未通过，合成降级预览页以保证有产物",
                    "runtime-check": "运行时冒烟：加载构建产物检查 console/page error",
                }
                is_recovery = phase in ("ai-repair", "stub", "vite-only", "fallback")
                recorder.emit(
                    AgentEventType.WARNING if is_recovery else AgentEventType.TOOL_CALL,
                    level=AgentEventLevel.WARNING if is_recovery else AgentEventLevel.INFO,
                    step_id=step.id,
                    message=labels.get(phase, f"构建阶段：{phase}"),
                    payload={"phase": phase},
                )

        # If a Figma design is attached to this project, feed it (render images +
        # IR) into the build so the generated React project matches the design.
        figma_frames = _load_figma_frames(project_id)
        if figma_frames:
            recorder.emit(
                AgentEventType.PROGRESS,
                step_id=step.id,
                message=f"按关联的 Figma 设计生成({len(figma_frames)} 个画板)",
                payload={"figma_frames": len(figma_frames)},
            )

        # Full-stack mode: if a shared API contract was synthesized for this
        # project, inject it so the generated frontend calls the REAL backend
        # (via window.__API_BASE__) instead of localStorage. Empty otherwise.
        from backend.services.code.fullstack import contract_service

        _ledger_row = contract_service.get_ledger(project_id)
        contract_block = ""
        if _ledger_row and _ledger_row.contract_status == "ready":
            contract_block = contract_service.render_contract_for_prompt(
                _ledger_row.get_api_contract(), include_db_schema=False
            )
            recorder.emit(
                AgentEventType.PROGRESS, step_id=step.id,
                message="全栈模式:已注入共享 API 契约,前端将调用真实后端(window.__API_BASE__)",
                payload={"fullstack": True},
            )

        # 二次开发·真实续改: seed the existing frontend project (when this run is
        # an iteration and a prior source exists) so the agent EDITS it per the
        # change instruction instead of regenerating from scratch.
        from backend.services.agent.workflows._iteration_support import (
            iteration_change,
            load_prior_source,
        )

        change = iteration_change(ctx)
        base_files = load_prior_source(project_id, "frontend") if change else {}
        if change and base_files:
            recorder.emit(
                AgentEventType.PROGRESS, step_id=step.id,
                message=f"续改模式：基于现有前端工程（{len(base_files)} 个文件）改动",
                payload={"mode": "iteration", "base_files": len(base_files)},
            )

        def _build(**overrides):
            kw = dict(
                requirement=project.requirement_input,
                requirements_doc=project.requirements_doc,
                development_flow=project.development_flow or "",
                documents_digest=_documents_digest(project),
                style_prompt=project.style_prompt or "",
                ui_baseline_prompt=project.ui_baseline_prompt or "",
                context_ledger=injected,
                contract_block=contract_block,
                figma_frames=figma_frames,
                base_files=base_files or None,
                change_instruction=(change or {}).get("instruction", ""),
                change_plan=(change or {}).get("plan_text", ""),
                on_event=on_event,
                is_cancelled=ctx.is_cancelled,
            )
            kw.update(overrides)
            return service.build_project(**kw)

        # P-B incremental batched build (env CODE_BUILD_BATCHES; default 1 → single
        # _build() in the else branch = pre-P-B behaviour). Fresh generation only (not
        # iteration) and only when the feature set is large enough to split. Batch 0 =
        # scaffold (full build); each later batch ADDS its feature subset through the
        # existing edit-mode path (base_files + change_instruction — no prompt change).
        # The acceptance verify→repair loop below then runs on the accumulated result.
        _batches = (
            _verify_support.split_batches(
                _verify_support.features_from_ledger(ledger.to_dict()),
                _verify_support.build_batches(),
            )
            if not change else []
        )
        if _batches:
            recorder.emit(
                AgentEventType.PROGRESS, step_id=step.id,
                message=f"分批增量构建:共 {len(_batches)} 批(每批聚焦一组功能,逐批累加)",
                payload={"batches": len(_batches)},
            )
            result = _build()  # batch 0: scaffold + first pass
            for _bi in range(1, len(_batches)):
                if not result.get("success") or ctx.is_cancelled():
                    break
                _acc = result.get("files") or {}
                if not _acc:
                    break
                recorder.emit(
                    AgentEventType.PROGRESS, step_id=step.id,
                    message=f"增量构建 第 {_bi + 1}/{len(_batches)} 批:"
                            + "、".join(f"[{f['id']}]" for f in _batches[_bi]),
                    payload={"batch": _bi + 1, "of": len(_batches)},
                )
                _wave = _build(
                    base_files=_acc,
                    change_instruction=_verify_support.render_feature_subset(
                        _batches[_bi], _bi, len(_batches)),
                    change_plan="在现有工程基础上增量新增本批功能,完整实现端到端,"
                                "保持已实现功能与构建不被破坏,不要整体重写。",
                )
                if _wave.get("error") == "cancelled":
                    return cancel_result(project_id)
                if _wave.get("success") and _wave.get("files"):
                    result = _wave
        else:
            result = _build()

        if result.get("error") == "cancelled":
            return cancel_result(project_id)
        # The container always synthesizes a previewable dist, so a hard failure
        # here means the agent produced no source at all (config/auth) — runtime
        # refunds it. A degraded-but-published run is NOT a failure.
        if not result.get("success"):
            raise RuntimeError(f"前端工程生成失败：{result.get('error') or '未知错误'}")

        # --- Verify -> repair loop (P0-A house rules / P0-B rubric review /
        # P1-C runtime smoke / P1-D features) -----------------------------------
        # Block ONLY on objective defects: deterministic house-rule errors, the
        # in-container runtime browser-smoke console errors, or the skeptical
        # evaluator's explicit blocking_issues. Subjective quality is advisory.
        # A blocking round triggers an edit-mode rebuild seeded with the current
        # source + a targeted brief, bounded by CODE_VERIFY_MAX_ROUNDS (default 2).
        max_verify_rounds = max(0, int(os.getenv("CODE_VERIFY_MAX_ROUNDS", "2") or 0))
        # A1 score gate + A3 refine/pivot — all env-gated (defaults preserve behaviour).
        _min_score = _verify_support.env_score_floor()
        _min_dims = _verify_support.env_dim_floors()
        _pivot_on = _verify_support.pivot_enabled()
        _score_history: list = []
        # P-A acceptance-driven iteration (env-gated; OFF → identical to pre-P-A loop).
        # When ON the loop keeps repairing — within the larger CODE_ITERATE_MAX_ROUNDS
        # budget — while functional features remain unmet (not only while blocking),
        # stopping on accepted / budget / stalled coverage.
        _to_acceptance = _verify_support.iterate_to_acceptance()
        _max_rounds = (
            _verify_support.iterate_max_rounds(max_verify_rounds)
            if _to_acceptance else max_verify_rounds
        )
        _stall = _verify_support.iterate_stall()
        _cov_history: list = []
        ledger_dict = ledger.to_dict()
        features = _verify_support.features_from_ledger(ledger_dict)
        _features_block = _verify_support.render_features_block(features)
        _reqs = [
            r for r in (ledger_dict.get("requirements") or [])
            if isinstance(r, dict) and r.get("id") and r.get("statement")
        ]
        _registry = "\n".join(f"- [{r['id']}] {r['statement']}" for r in _reqs)

        def _review_one(digest: str, house_report: str, runtime_report: str, lens: str):
            """ONE skeptical review on the reliable text lane. No DB work, so it is
            safe to run concurrently; charging happens in _review_panel before fan-out."""
            return service.review_project(
                source_digest=digest,
                requirements_registry=_registry,
                style_prompt=project.style_prompt or "",
                features_block=_features_block,
                house_rules_report=house_report,
                runtime_report=runtime_report,
                extra_directive=lens,
            )

        def _review_panel(files, house_report, runtime_report):
            """N independent reviews (rotating lenses) -> majority consensus (②a). Charge
            per reviewer up-front (DB, this thread), then run the model calls CONCURRENTLY."""
            n = max(1, int(os.getenv("CODE_REVIEW_PANEL", "1") or 1))
            if not (_reqs and files and gate_available()):
                return None
            lenses = _verify_support.REVIEW_LENSES_FRONTEND
            digest = _source_digest(files)
            thunks = []
            for i in range(n):
                if not charge(
                    user_id=ctx.user_id, amount=pricing.CODE_PROJECT_REVIEW,
                    operation="code_project_review", resource_type="agent_run",
                    resource_id=ctx.run_id, description="frontend project review",
                    team_id=ctx.team_id,
                ):
                    break
                _lens = lenses[i % len(lenses)] if n > 1 else ""
                thunks.append(
                    lambda lens=_lens: _review_one(digest, house_report, runtime_report, lens)
                )
            out = _verify_support.run_reviewers(thunks)
            return _verify_support.aggregate_reviews([r for r in out if r])

        # Verify each artifact exactly ONCE; a repair is adopted only if it did not
        # REGRESS (P1-1) — otherwise revert to the prior, better artifact.
        def _verify(res):
            _files = res.get("files") or {}
            _violations = house_rules.check_frontend(_files)
            _rt_check = res.get("runtime_check")
            _rev = _review_panel(
                _files, house_rules.render_report(_violations),
                _verify_support.render_runtime_report(_rt_check),
            )
            _feats, _feat_stats = _verify_support.apply_feature_results(
                features, (_rev or {}).get("feature_results")
            )
            return _verify_support.Verification(
                house_rule_errors=house_rules.errors(_violations),
                house_rule_warnings=house_rules.warnings(_violations),
                runtime_errors=_verify_support.runtime_errors(_rt_check),
                runtime_check=_rt_check, review=_rev, features=_feats,
                feature_stats=_feat_stats,
                min_weighted_score=_min_score, min_dim_scores=_min_dims,
            )

        verification = _verify(result)
        for _round in range(_max_rounds + 1):
            _ws = verification.weighted_score
            if _ws is not None:
                _score_history.append(_ws)
            _cov_history.append(_verify_support.functional_coverage(verification.features)[0])
            _blocking = verification.blocking
            _stop, _stop_why = _verify_support.should_stop(
                verification, _round, _max_rounds,
                to_acceptance=_to_acceptance, coverage_history=_cov_history, stall=_stall,
            )
            recorder.emit(
                AgentEventType.WARNING if _blocking else AgentEventType.PROGRESS,
                level=AgentEventLevel.WARNING if _blocking else AgentEventLevel.INFO,
                step_id=step.id,
                message=f"质量验证(第 {_round} 轮):{verification.summary_line()}"
                        + ("" if _stop else "，启动定向修复重建")
                        + (f"({_stop_why})" if _stop and _stop_why and _to_acceptance else ""),
                payload={
                    "round": _round, "blocking": _blocking,
                    "house_rules": house_rules.summarize(
                        list(verification.house_rule_errors) + list(verification.house_rule_warnings)
                    ),
                    "runtime_errors": len(verification.runtime_errors),
                    "feature_stats": verification.feature_stats,
                    "verdict": (verification.review or {}).get("verdict"),
                },
            )
            if _stop:
                break
            if ctx.is_cancelled():
                return cancel_result(project_id)
            # A3: if the rubric score stalled across rounds, escalate refine -> pivot.
            _pivot = _pivot_on and _verify_support.should_pivot(_score_history)
            if _pivot:
                recorder.emit(
                    AgentEventType.PROGRESS, step_id=step.id,
                    message=f"质量分连续未改善({_score_history[-2:]})，本轮放宽为允许较大重构(pivot)",
                )
            _change_plan = _verify_support.REPAIR_PIVOT_PLAN if _pivot else (
                "仅修复上述硬性问题(房规违规 / 运行时报错 / 缺失的核心功能),"
                "保持其余文件不变,不要重写整个工程。"
            )
            _repaired = service.build_project(
                requirement=project.requirement_input,
                requirements_doc=project.requirements_doc,
                development_flow=project.development_flow or "",
                documents_digest=_documents_digest(project),
                style_prompt=project.style_prompt or "",
                ui_baseline_prompt=project.ui_baseline_prompt or "",
                context_ledger=injected,
                contract_block=contract_block,
                figma_frames=figma_frames,
                base_files=result.get("files") or {},
                change_instruction=verification.repair_instruction(),
                change_plan=_change_plan,
                on_event=on_event,
                is_cancelled=ctx.is_cancelled,
            )
            if _repaired.get("error") == "cancelled":
                return cancel_result(project_id)
            if not _repaired.get("success"):
                recorder.emit(
                    AgentEventType.WARNING, level=AgentEventLevel.WARNING, step_id=step.id,
                    message="修复重建未产出有效结果,沿用上一轮产物。",
                )
                break
            # P1-1 regression guard: verify the repaired artifact, adopt only if it did
            # not regress; otherwise revert to the prior (better) artifact and stop.
            _cand = _verify(_repaired)
            _regressed, _why = _verify_support.repair_regressed(verification, _cand)
            if _regressed:
                recorder.emit(
                    AgentEventType.WARNING, level=AgentEventLevel.WARNING, step_id=step.id,
                    message=f"本轮修复出现回归,已回退到上一轮产物:{_why}",
                    payload={"regressed": True, "reason": _why, "round": _round},
                )
                break
            result, verification = _repaired, _cand

        degraded_reason = result.get("degraded_reason")
        _DEGRADED_LABELS = {
            "ai-repair": "首轮构建未通过，经 AI 定向修复后构建成功",
            "stub": "构建经确定性补桩（缺失模块占位）后成功，部分模块为占位实现",
            "vite-only": "跳过类型检查后由 Vite 构建成功，可能存在未解决的类型问题",
            "fallback": "多轮修复后构建仍未通过，已合成降级预览页（源码可下载）",
        }
        if result.get("degraded"):
            recorder.emit(
                AgentEventType.WARNING,
                level=AgentEventLevel.WARNING,
                step_id=step.id,
                message=f"前端工程以降级方式产出：{_DEGRADED_LABELS.get(degraded_reason, degraded_reason)}",
                payload={"degraded_reason": degraded_reason},
            )

        usage = result.get("usage") or {}
        src_files = result.get("files") or {}
        n_src = len(src_files)
        n_dist = len(result.get("dist_files") or {})
        # Raster assets generated by the image-assets skill (Codex -> image model).
        _ASSET_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".ico", ".bmp")
        n_assets = sum(1 for key in src_files if key.lower().endswith(_ASSET_EXTS))

        # Asset-lane diagnostics: explain a "no images" run instead of failing
        # silently — distinguish image-not-rebuilt / no-key / agent-didn't-call.
        lane = result.get("asset_lane") or {}
        if not lane.get("codex_available", True):
            recorder.emit(
                AgentEventType.WARNING, level=AgentEventLevel.WARNING, step_id=step.id,
                message="fe-agent 镜像内未找到 Codex CLI：未生成图片资源。"
                "请重建镜像后重试：docker compose --profile setup build（或 "
                "docker build -t fe-agent:latest backend/docker/fe-agent）。",
                payload={"asset_lane": lane},
            )
        elif not lane.get("openai_key", True):
            recorder.emit(
                AgentEventType.WARNING, level=AgentEventLevel.WARNING, step_id=step.id,
                message="未配置 OPENAI_API_KEY：未生成图片资源（已用 CSS/SVG 兜底）。",
                payload={"asset_lane": lane},
            )
        elif lane.get("calls", 0) == 0:
            recorder.emit(
                AgentEventType.WARNING, level=AgentEventLevel.WARNING, step_id=step.id,
                message="本次 Claude 未触发图片资源生成（gen-assets 调用 0 次）：未产出图片资源。",
                payload={"asset_lane": lane},
            )
        elif n_assets:
            recorder.emit(
                AgentEventType.PROGRESS, step_id=step.id,
                message=f"图片资源生成：gen-assets 调用 {lane.get('calls')} 次，产出 {n_assets} 张图片资源。",
                payload={"asset_lane": lane},
            )

        # --- Verification artifact (P0-B): the final round's combined verdict --
        # rubric scores + per-feature results + house-rule / runtime findings.
        # Distinct ref type so it never collides with the publish step's
        # project-meta artifact (the frontend preview picker .find()s by ref type).
        if verification is not None:
            step.add_artifact(
                AgentArtifactType.JSON, "前端工程验收与质量验证",
                content_json=verification.to_record(),
                filename="frontend_project_verification.json",
                domain_ref_type="code_frontend_project_review", domain_ref_id=project_id,
            )
            # Persist ONE online quality sample so generation quality is measurable
            # over time (eval framework, P0-B). Fail-soft inside the helper — a
            # metrics write must never sink the run. ``_round`` leaks from the
            # verify loop above as the count of verify passes that ran (1 = no repair).
            from backend.services.code.quality_metrics import record_quality_sample

            record_quality_sample(
                run_id=ctx.run_id, project_id=project_id, user_id=ctx.user_id,
                team_id=ctx.team_id, lane="frontend", verification=verification,
                verify_rounds=_round + 1, degraded_reason=degraded_reason,
            )
        # Runtime-smoke screenshot (P1-C): a real picture of the built app, so a
        # human (and a future iteration run) can SEE the result, not just read code.
        _shot_bytes = result.get("runtime_screenshot")
        if _shot_bytes:
            step.add_artifact(
                AgentArtifactType.IMAGE, "运行时冒烟截图",
                filename="runtime_screenshot.png", mime_type="image/png",
                write_file=True, content_bytes=_shot_bytes,
                domain_ref_type="code_frontend_runtime_shot", domain_ref_id=project_id,
            )

        step.model_response = (result.get("summary") or "")[:8000]
        db.session.commit()
        recorder.emit(
            AgentEventType.MODEL_RESPONSE, step_id=step.id, message="agent 完成",
            payload={"summary": result.get("summary"), "usage": usage,
                     "cost_usd": result.get("cost_usd"),
                     "degraded_reason": degraded_reason},
        )
        _degraded_note = f"（降级：{_DEGRADED_LABELS.get(degraded_reason, degraded_reason)}）" if result.get("degraded") else ""
        _asset_note = f"，其中 {n_assets} 张为 Codex 生成的图片资源" if n_assets else ""
        step.set_output(
            output_summary=f"已生成完整前端工程：{n_src} 个源码文件{_asset_note}，{n_dist} 个构建产物{_degraded_note}。{result.get('summary', '')}".strip(),
            reasoning_summary="沙箱容器内 Claude Code 自主创建多文件 React/Vite/TS 工程（需要真实图片时经 image-assets 技能触发 Codex 生成位图资源），并经自愈构建梯队（AI 修复 → 确定性补桩 → Vite 兜底 → 合成降级页）确保产出可预览 dist。",
            self_check=f"源码 {n_src} 文件（图片资源 {n_assets} 张）；dist {n_dist} 文件；cost≈${result.get('cost_usd')}；降级={degraded_reason or '无'}；验证：{verification.summary_line() if verification else '—'}",
            next_action="发布并提供预览。",
        )
    completed += 1
    progress("publish")

    # --- Step 3: Publish (source zip + previewable dist) ---------------------
    with recorder.step(
        "fe_publish", "发布 Agent", "publisher", 3, input_summary="保存源码 zip 与可预览 dist"
    ) as step:
        files = result.get("files") or {}
        dist_files = result.get("dist_files") or {}
        # P2-E: guarantee a navigable docs/ + AGENTS.md scaffold in the published
        # source (adds only what the agent didn't write), so iteration / repair
        # runs (re-seeded from this source) get a knowledge base + golden
        # principles in-repo. Source-only (not served), so dist is untouched.
        files = {**files, **scaffold.ensure_scaffold(files, kind="frontend", contract_block=contract_block)}

        # Source -> downloadable zip artifact. Values are bytes (binary-safe), so
        # generated raster assets survive into the zip / GitHub commit intact.
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for rel, content in files.items():
                archive.writestr(rel, content or b"")
        step.add_artifact(
            AgentArtifactType.TEXT, "前端工程源码（zip）",
            filename="frontend_project.zip", mime_type="application/zip",
            write_file=True, content_bytes=buffer.getvalue(),
            domain_ref_type="code_frontend_project_zip", domain_ref_id=project_id,
        )

        # Built dist -> on-disk site dir for iframe preview (preserve assets/ tree).
        # Bytes (binary-safe) so bundled images render in the preview iframe.
        site_dir = agent_run_dir(ctx.run_id) / "site"
        site_dir.mkdir(parents=True, exist_ok=True)
        for rel, content in dist_files.items():
            target = site_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content or b"")
        preview_url = f"/api/agent/runs/{ctx.run_id}/site/index.html"

        step.add_artifact(
            AgentArtifactType.JSON, "前端工程元数据",
            content_json={
                "source_files": sorted(files.keys()),
                "dist_files": sorted(dist_files.keys()),
                "preview_url": preview_url,
                "cost_usd": result.get("cost_usd"),
                "usage": result.get("usage"),
                "summary": result.get("summary"),
                "delivery": "multi-file-project",
                "degraded": result.get("degraded", False),
                "degraded_reason": result.get("degraded_reason"),
            },
            filename="frontend_project_meta.json",
            preview_url=preview_url,
            domain_ref_type="code_frontend_project_meta", domain_ref_id=project_id,
        )
        step.set_context(snapshot={"injected_text": "", "ledger": ledger.to_dict()})
        step.set_output(
            output_summary="前端工程已发布：可在右侧预览构建结果或下载源码 zip。",
            reasoning_summary="把源码打包为 zip artifact，构建产物 dist 写入站点目录供 iframe 预览。",
            self_check=f"源码 {len(files)} 文件；dist {len(dist_files)} 文件；preview={preview_url}",
            next_action="在预览面板交互验证，或下载源码。",
        )
    completed += 1
    progress("done")

    return {"status": AgentRunStatus.COMPLETED, "resource_id": project_id}
