"""
Built-in node contracts — the existing generation stages as typed canvas nodes.

Each contract maps a real stage (today hardcoded inside ``code_workflow`` and the
full-stack workflows) to a typed, composable node: declared input/output ports,
the prompt it is pinned to, the Session-Context-Ledger sections it reads/writes,
its executor and its pricing key. Wiring these on the canvas is how a user builds
a *custom* pipeline out of the platform's real stages.

These are the seed/fallback source (mirrors ``prompts/defaults.py``); a later
increment lets admins override them in Mongo. Pure data — no Flask, no DB.

Prompt keys follow the ``code/<file>.txt`` convention used by ``prompt_store``.
Pricing keys are the *names* of constants in ``services/pricing.py`` (resolved +
validated at use time, never the numeric value — so the price table stays the one
source of truth).
"""
from backend.services.agent.contracts.node_contract import NodeContract, Port, PromptRef

# Ledger sections a stage establishes, reused across several contracts.
_REQ_WRITES = ["requirements", "glossary", "decisions", "tech_stack"]


def _build() -> list[NodeContract]:
    return [
        # —— linear design stages (text) ——
        NodeContract(
            node_type="requirements",
            role="generator",
            inputs=[
                Port("brief", "core:user_text", required=True),
                Port("ledger", "core:context_ledger"),
            ],
            outputs=[Port("doc", "code:requirements_doc")],
            context_reads=["project", "requirements", "open_questions"],
            context_writes=_REQ_WRITES,
            prompt_ref=PromptRef("code/requirements_prompt.txt"),
            review_gate=True,
            pricing_key="CODE_FULL_GENERATION",
            executor="stage_text",
        ),
        NodeContract(
            node_type="flow",
            role="generator",
            inputs=[
                Port("requirements", "code:requirements_doc", required=True),
                Port("ledger", "core:context_ledger"),
            ],
            outputs=[Port("doc", "code:development_flow")],
            context_reads=["project", "requirements", "tech_stack"],
            context_writes=["decisions", "tech_stack", "constraints"],
            prompt_ref=PromptRef("code/development_flow_prompt.txt"),
            review_gate=True,
            pricing_key="CODE_FULL_GENERATION",
            executor="stage_text",
        ),
        NodeContract(
            node_type="documents",
            role="generator",
            inputs=[
                Port("flow", "code:development_flow", required=True),
                Port("ledger", "core:context_ledger"),
            ],
            outputs=[Port("docs", "code:document_set")],
            context_reads=["project", "requirements", "tech_stack", "decisions"],
            context_writes=["decisions"],
            prompt_ref=PromptRef("code/document_split_prompt.txt"),
            review_gate=True,
            pricing_key="CODE_FULL_GENERATION",
            executor="stage_text",
        ),
        NodeContract(
            node_type="style",
            role="generator",
            inputs=[
                Port("documents", "code:document_set", required=True),
                Port("ledger", "core:context_ledger"),
            ],
            outputs=[Port("doc", "code:style_doc")],
            context_reads=["project", "tech_stack", "decisions"],
            context_writes=["decisions", "tech_stack"],
            prompt_ref=PromptRef("code/style_prompt.txt"),
            review_gate=True,
            pricing_key="CODE_FULL_GENERATION",
            executor="stage_text",
        ),
        # —— UI preview (image, no text prompt) ——
        NodeContract(
            node_type="preview",
            role="generator",
            inputs=[Port("style", "code:style_doc", required=True)],
            outputs=[Port("preview", "code:ui_preview")],
            context_reads=["project", "tech_stack"],
            prompt_ref=None,
            review_gate=False,
            pricing_key="CODE_FULL_GENERATION",
            executor="stage_preview",
        ),
        # —— full-stack build lanes ——
        NodeContract(
            node_type="fe_build",
            role="generator",
            inputs=[
                Port("preview", "code:ui_preview", required=True),
                Port("api_contract", "code:api_contract"),
            ],
            outputs=[Port("frontend", "code:frontend_project")],
            context_reads=["project", "tech_stack", "decisions"],
            prompt_ref=PromptRef("code/frontend_project_prompt.txt"),
            review_gate=False,
            pricing_key="CODE_FRONTEND_PROJECT_GENERATION",
            executor="container_fe",
        ),
        NodeContract(
            node_type="be_build",
            role="generator",
            inputs=[Port("api_contract", "code:api_contract", required=True)],
            outputs=[Port("backend", "code:backend_project")],
            context_reads=["project", "tech_stack", "decisions"],
            prompt_ref=PromptRef("code/backend_project_prompt.txt"),
            review_gate=False,
            pricing_key="CODE_BACKEND_PROJECT_GENERATION",
            executor="container_be",
        ),
        NodeContract(
            node_type="mw_provision",
            role="generator",
            inputs=[Port("api_contract", "code:api_contract", required=True)],
            outputs=[Port("middleware", "code:middleware_manifest")],
            context_reads=["project", "tech_stack"],
            prompt_ref=PromptRef("code/middleware_prompt.txt"),
            review_gate=False,
            pricing_key="CODE_MIDDLEWARE_PROVISIONING",
            executor="provision_mw",
        ),
        # —— deploy (backend-centric) ——
        # deploy_service runs the BACKEND container + registers /app/<pid>/api; the
        # frontend is served statically at /preview/<pid>/ (NOT a deploy input). So a
        # deploy needs `backend` (required) + `middleware` (only when DB schema is
        # involved). "Frontend only" → no deploy node (the build is the preview);
        # "backend only" → wire just `backend`; "full-stack" → wire all lanes.
        NodeContract(
            node_type="deploy",
            role="publisher",
            inputs=[
                Port("backend", "code:backend_project", required=True),
                Port("middleware", "code:middleware_manifest"),
            ],
            outputs=[Port("deployment", "code:deployment")],
            prompt_ref=None,
            review_gate=False,
            pricing_key="CODE_FULLSTACK_DEPLOY",
            executor="deploy",
        ),
    ]


_ordered: list[NodeContract] | None = None
_index: dict[str, NodeContract] | None = None


def iter_default_node_contracts() -> list[NodeContract]:
    """Return every built-in node contract."""
    global _ordered
    if _ordered is None:
        _ordered = _build()
    return _ordered


def get_default_contract(node_type: str) -> NodeContract | None:
    """Return the built-in contract for ``node_type``, or ``None``."""
    global _index
    if _index is None:
        _index = {c.node_type: c for c in iter_default_node_contracts()}
    return _index.get(node_type)
