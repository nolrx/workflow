from backend.services.code.generation_service import CodeGenerationService
from backend.services.prompt_library import (
    PROMPT_RECIPE_EXAMPLES,
    PROMPT_PREFIXES,
    SYSTEM_PROMPT_ASSEMBLY_GUIDE,
    compose_recipe_prompt,
    compose_system_prompt,
    route_prefixes,
)


def test_prompt_library_contains_all_internet_role_prefixes():
    expected = {
        "strategy",
        "product_pm",
        "ai_product",
        "prompt_engineering",
        "ux_ui",
        "user_research",
        "pmo_delivery",
        "frontend",
        "backend",
        "architecture",
        "qa_testing",
        "devops_sre",
        "data_analysis",
        "data_engineering",
        "ml_ai_engineering",
        "security",
        "trust_safety",
        "operations",
        "growth_marketing",
        "sales_cs",
        "content_creative",
        "legal_compliance",
        "hr_org",
        "customer_support",
    }

    assert set(PROMPT_PREFIXES) == expected


def test_compose_system_prompt_orders_base_roles_and_contract():
    prompt = compose_system_prompt("product_pm", ["ux_ui", "qa_testing"])

    assert prompt.index("## BASE_SYSTEM_PREFIX") < prompt.index("## PREFIX_PRODUCT_PM")
    assert prompt.index("## PREFIX_PRODUCT_PM") < prompt.index("## PREFIX_UX_UI")
    assert prompt.index("## PREFIX_UX_UI") < prompt.index("## PREFIX_QA_TESTING")
    assert prompt.index("## PREFIX_QA_TESTING") < prompt.index("## OUTPUT_CONTRACT")
    assert "不要只描述功能" in prompt
    assert "不要只验证" in prompt


def test_route_prefixes_selects_matching_roles():
    route = route_prefixes("帮我设计一个 AI Agent 的系统提示词、RAG 工作流和安全兜底")

    assert route.primary_role in {"ai_product", "prompt_engineering"}
    assert "security" in route.selected_prefixes
    assert route.recommended_system_prompt_order[0] == "BASE_SYSTEM_PREFIX"
    assert route.recommended_system_prompt_order[-1] == "OUTPUT_CONTRACT"


def test_product_requirement_recipe_includes_product_design_and_testing():
    prompt = compose_recipe_prompt("product_requirement")

    assert "PREFIX_PRODUCT_PM" in prompt
    assert "PREFIX_UX_UI" in prompt
    assert "PREFIX_QA_TESTING" in prompt
    assert "帮我设计一个用户积分系统" in PROMPT_RECIPE_EXAMPLES["product_requirement"]["tasks"]
    assert "当用户任务明确时" in SYSTEM_PROMPT_ASSEMBLY_GUIDE


def test_code_requirement_context_injects_prompt_library_prefixes():
    service = CodeGenerationService()
    prompt, fallback = service._requirements_context("做一个提示词库")

    assert "## BASE_SYSTEM_PREFIX" in prompt
    assert "## PREFIX_PRODUCT_PM" in prompt
    assert "用户原始需求：" in prompt
    assert "做一个提示词库" in prompt
    assert fallback.startswith("# 软件需求文档")
