"""Pytest gate: degraded-mode (provider-unconfigured / model-failure) fallbacks
must themselves satisfy the SAME stage contracts as the live model output.

A fallback that drops FR/NFR numbering, hard-codes a stale stack, or emits a
non-baseline document type silently poisons every downstream stage (each doc is
the next stage's context). These tests assert the local fallbacks are minimal
BUT legal — the BMAD "each artifact feeds the next stage" invariant holds even
when the model is unavailable.
"""
from backend.services.code.generation_service import CodeGenerationService

_REQ_SECTIONS = [
    "## 产品定位", "## 目标用户", "## 核心场景", "## 功能范围", "## 用户流程",
    "## 权限与账户", "## 数据对象", "## 非功能要求", "## 技术架构建议", "## 边界与待确认问题",
]
_FLOW_SECTIONS = [
    "## 技术假设", "## 模块拆分", "## 数据设计", "## 接口设计", "## 前端页面/状态",
    "## 后端服务", "## AI/提示词链路", "## 开发里程碑", "## 验收标准", "## 风险清单",
]
# The six baseline split-document types the document_split contract mandates.
_BASELINE_DOC_TYPES = {
    "product_spec", "frontend_spec", "backend_spec",
    "data_model", "prompt_spec", "acceptance_plan",
}


def test_requirements_fallback_is_contract_shaped():
    doc = CodeGenerationService._requirements_fallback("做一个待办事项应用")
    missing = [s for s in _REQ_SECTIONS if s not in doc]
    assert not missing, f"requirements fallback missing sections: {missing}"
    # FR/NFR traceability anchors must be present (the chain depends on them).
    assert "FR1" in doc and "FR3" in doc, "requirements fallback lacks FR numbering"
    assert "NFR1" in doc and "NFR2" in doc, "requirements fallback lacks NFR numbering"
    # Must NOT promise a platform the implementation stage can't deliver.
    assert "移动 App" not in doc and "桌面应用" not in doc, \
        "requirements fallback must narrow to a Web app (no native-platform promise)"


def test_development_flow_fallback_is_contract_shaped():
    doc = CodeGenerationService._development_flow_fallback()
    missing = [s for s in _FLOW_SECTIONS if s not in doc]
    assert not missing, f"flow fallback missing sections: {missing}"
    assert "M1" in doc and "MS1" in doc, "flow fallback lacks M/MS numbering"
    assert "覆盖 FR" in doc, "flow fallback lacks FR/NFR traceability annotations"
    # No hard-coded stack — the backend stack is locked at the contract stage.
    assert "Flask" not in doc, "flow fallback must not hard-code Flask"


def test_documents_fallback_emits_exactly_the_six_baseline_types():
    docs = CodeGenerationService._fallback_documents(None, "需求文档正文", "开发流程正文")
    types = {d["document_type"] for d in docs}
    assert types == _BASELINE_DOC_TYPES, (
        f"document fallback must emit exactly the 6 baseline types; got {sorted(types)} "
        f"(missing {sorted(_BASELINE_DOC_TYPES - types)}, extra {sorted(types - _BASELINE_DOC_TYPES)})"
    )
    # Previously emitted a non-baseline 'development_plan' and omitted 'data_model'.
    assert "development_plan" not in types
    assert "data_model" in types
    # order_index must be unique + contiguous so downstream ordering is stable.
    assert sorted(d["order_index"] for d in docs) == list(range(len(docs)))
