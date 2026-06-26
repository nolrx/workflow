"""
Fold a canvas stage's produced doc back into the consensus ledger, per its kind.

Mirrors the linear pipeline's per-stage extraction (``code_workflow``) — reusing
its markdown helpers — so a MULTI-STAGE canvas accumulates the same consensus
(product定位 / 需求条目 / 术语 / 关键决策 / 技术栈约束) and later stage nodes build
on it instead of drifting. Without this, a canvas stage only *reads* the ledger
into its prompt but never *writes* what it established back.

Pure orchestration over ``ContextLedger.merge`` + the code_workflow md helpers
(lazily imported to avoid an import cycle). No Flask/DB here — the caller persists.
"""


def merge_stage_doc_into_ledger(node_type: str, doc: str, ledger, *, source_step: str) -> bool:
    """Extract + merge a stage doc's establishments into ``ledger`` by stage kind.

    Returns True if anything was merged (so the caller knows to persist). Mirrors
    the requirements / flow / documents / style extraction the linear pipeline does.
    """
    from backend.services.agent.workflows.code_workflow import (
        _bullets,
        _md_sections,
        _req_items_from_section,
        _section_body,
    )

    sections = _md_sections(doc or "")
    if not sections and not (doc or "").strip():
        return False

    if node_type == "requirements":
        one_liner = _bullets(_section_body(sections, "产品定位", "定位"), 1)
        req_items = _req_items_from_section(
            _section_body(sections, "功能范围")
        ) + _req_items_from_section(_section_body(sections, "非功能"))
        ledger.merge(
            project={
                "one_liner": one_liner[0] if one_liner else "",
                "target_users": _bullets(_section_body(sections, "目标用户"), 5),
                "scope_in": _bullets(_section_body(sections, "功能范围", "功能"), 8),
            },
            requirements_add=req_items,
            tech_stack={"constraints": _bullets(_section_body(sections, "技术架构", "架构"), 6)},
            open_questions=_bullets(_section_body(sections, "待确认", "边界"), 6),
            provenance_entry={
                "step": source_step,
                "agent_key": "requirements",
                "fields_touched": ["project", "requirements", "tech_stack.constraints"],
            },
        )
        return True

    if node_type == "flow":
        ledger.merge(
            tech_stack={"constraints": _bullets(_section_body(sections, "技术假设", "技术"), 6)},
            provenance_entry={
                "step": source_step,
                "agent_key": "flow",
                "fields_touched": ["tech_stack.constraints"],
            },
        )
        return True

    if node_type == "documents":
        # No split-doc list on the canvas; use the doc's section titles as glossary
        # terms (a faithful-enough approximation of the linear pipeline's per-doc terms).
        terms = [t for t in sections if t][:20]
        ledger.merge(
            glossary_add=[
                {"term": t, "definition": "开发文档章节", "source_step": "documents"}
                for t in terms
            ],
            provenance_entry={
                "step": source_step,
                "agent_key": "documents",
                "fields_touched": ["glossary"],
            },
        )
        return bool(terms)

    if node_type == "style":
        tone = _bullets(_section_body(sections, "UI 基调", "基调", "视觉定位"), 1)
        ledger.merge(
            decisions_add=[
                {
                    "id": "ui-tone",
                    "statement": tone[0] if tone else "已确立 UI 风格基调",
                    "rationale": "风格阶段产出，供下游遵循",
                    "source_step": "style",
                }
            ],
            provenance_entry={
                "step": source_step,
                "agent_key": "style",
                "fields_touched": ["decisions"],
            },
        )
        return True

    return False
