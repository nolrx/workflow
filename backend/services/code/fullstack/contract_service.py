"""
Shared API contract synthesis — the linchpin that lets the frontend, backend and
middleware runs agree on ONE API surface.

From the project's development flow (the ``## 接口设计`` / ``## 数据设计`` /
``## 后端服务`` / ``## AI/提示词链路`` / ``## 技术假设`` sections) plus the backend
documents, this synthesizes:

  * an OpenAPI 3.x contract (the endpoints both services bind to), and
  * a middleware manifest (datastores / cache / env the backend needs).

It is written ONCE into ``CodeProjectLedger`` (project-keyed) by the
orchestration endpoint, before the three concurrent runs start, so they all read
the same frozen contract — no cross-run write race. A text-model call does the
synthesis; when no provider is configured it degrades to a deterministic
extraction from the flow markdown, so the pipeline never hard-blocks on AI.
"""
import json
import logging
import re
from typing import Optional

from backend.extensions import db
from backend.models.agent import AgentRun
from backend.models.code import CodeProject
from backend.models.code.fullstack import CodeProjectLedger, ContractStatus
from backend.services.agent.context_ledger import ContextLedger, seed_from_inputs
from backend.services.prompts import prompt_store

logger = logging.getLogger(__name__)

# Backend-relevant document types from the split step (others are FE/product).
_BACKEND_DOC_TYPES = ("backend_spec", "data_model", "prompt_spec")
_MAX_DOC_CHARS = 2000
_MAX_DIGEST_CHARS = 14_000
_MAX_FLOW_CHARS = 16_000

# Keyword → datastore type, for the deterministic fallback manifest.
_DATASTORE_HINTS = {
    "postgres": "postgres",
    "postgresql": "postgres",
    "mysql": "mysql",
    "sqlite": "sqlite",
    "mongo": "mongodb",
    "mongodb": "mongodb",
}
_CACHE_HINTS = ("redis", "memcached")
_QUEUE_HINTS = ("kafka", "rabbitmq", "celery", "队列", "消息队列")

# Language hints scanned in the flow's tech assumptions for the fallback stack.
_LANG_HINTS = [
    ("python", "python"), ("fastapi", "python"), ("flask", "python"), ("django", "python"),
    ("node", "node"), ("express", "node"), ("nest", "node"), ("typescript", "node"),
    ("koa", "node"), ("fastify", "node"),
    ("golang", "go"), ("go ", "go"), ("gin", "go"), ("fiber", "go"),
    ("java", "java"), ("spring", "java"),
]


def _md_sections(markdown: str) -> dict:
    """Split a markdown doc into ``{header: body}`` keyed by ``##`` headers."""
    sections: dict[str, str] = {}
    current = None
    buf: list[str] = []
    for line in (markdown or "").splitlines():
        m = re.match(r"^#{2,4}\s+(.*)$", line.strip())
        if m:
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = m.group(1).strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


def _section(sections: dict, *keywords: str) -> str:
    """Return the first section whose header contains any of the keywords."""
    for header, body in sections.items():
        if any(kw in header for kw in keywords):
            return body
    return ""


def backend_documents_digest(project: CodeProject) -> str:
    """Concatenate the backend-relevant split documents (capped)."""
    parts: list[str] = []
    for document in project.documents.all():
        if document.document_type not in _BACKEND_DOC_TYPES:
            continue
        body = (document.content or "")[:_MAX_DOC_CHARS]
        parts.append(f"## {document.title} ({document.document_type})\n{body}")
    return "\n\n".join(parts)[:_MAX_DIGEST_CHARS]


def _extract_json(text: str) -> Optional[dict]:
    """Tolerant single-object JSON parse (code-fence / prefix tolerant)."""
    if not text:
        return None
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL | re.IGNORECASE)
    if fenced:
        cleaned = fenced.group(1).strip()
    if not cleaned.startswith("{"):
        match = re.search(r"(\{.*\})", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(1)
    try:
        value = json.loads(cleaned)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        logger.warning("contract synthesis: failed to parse model JSON output")
        return None


def _fallback_contract(project: CodeProject) -> dict:
    """Deterministic contract when no text provider is available.

    Extracts the interface / data / backend sections verbatim as the api_summary,
    infers datastores/cache from keyword scan, and guesses the stack from the
    tech-assumptions section. Good enough to keep the pipeline moving; the agents
    then implement against the raw section text.
    """
    flow = project.development_flow or ""
    sections = _md_sections(flow)
    api_text = _section(sections, "接口设计", "接口", "API")
    data_text = _section(sections, "数据设计", "数据")
    backend_text = _section(sections, "后端服务", "后端")
    ai_text = _section(sections, "AI", "提示词")
    tech_text = _section(sections, "技术假设", "技术")

    scan = (flow + " " + backend_documents_digest(project)).lower()
    datastores = []
    for hint, dtype in _DATASTORE_HINTS.items():
        if hint in scan and not any(d["type"] == dtype for d in datastores):
            datastores.append({"type": dtype, "purpose": "主数据存储", "entities": []})
    if not datastores:
        datastores = [{"type": "postgres", "purpose": "主数据存储", "entities": []}]
    cache = None
    for hint in _CACHE_HINTS:
        if hint in scan:
            cache = {"type": "redis", "purpose": "缓存 / 会话"}
            break
    queue = None
    for hint in _QUEUE_HINTS:
        if hint in scan:
            queue = {"type": "queue", "purpose": "异步任务"}
            break

    language, framework = "node", "express"
    for hint, lang in _LANG_HINTS:
        if hint in tech_text.lower() or hint in scan:
            language = lang
            framework = {
                "python": "fastapi", "node": "express", "go": "gin", "java": "spring",
            }.get(lang, framework)
            break

    api_summary = "\n\n".join(
        block for block in (
            f"## 接口设计\n{api_text}" if api_text else "",
            f"## 数据设计\n{data_text}" if data_text else "",
            f"## 后端服务\n{backend_text}" if backend_text else "",
            f"## AI/提示词链路\n{ai_text}" if ai_text else "",
        ) if block
    )[:_MAX_FLOW_CHARS]

    env = [
        {"name": "PORT", "purpose": "HTTP 监听端口(部署时注入)"},
        {"name": "DATABASE_URL", "purpose": "主数据库连接串(部署时注入)"},
    ]
    if cache:
        env.append({"name": "REDIS_URL", "purpose": "Redis 连接串(部署时注入)"})

    return {
        "openapi": {},  # no structured OpenAPI without a model; agents use api_summary
        "api_summary": api_summary or "(开发流程未给出明确接口设计,后端按文档与共识实现)",
        "tech_stack": {
            "language": language,
            "framework": framework,
            "backend": tech_text[:600],
        },
        "middleware": {
            "datastores": datastores,
            "cache": cache,
            "queue": queue,
            "env": env,
            "schema_sql": "",
            "notes": "(确定性回退清单:未配置文本模型,按关键词推断)",
        },
        "_degraded": True,
    }


def synthesize_contract(project: CodeProject) -> dict:
    """Run the model synthesis (or deterministic fallback) → contract dict.

    Returns ``{openapi, api_summary, tech_stack, middleware, _degraded?}``.
    """
    from backend.services.ai import get_text_provider

    provider = get_text_provider()
    if not provider or not provider.is_configured():
        return _fallback_contract(project)

    flow = (project.development_flow or "")[:_MAX_FLOW_CHARS]
    documents = backend_documents_digest(project)
    requirements = (project.requirements_doc or "")[:_MAX_FLOW_CHARS]
    try:
        template = prompt_store.get("code/contract_synthesis_prompt.txt")
    except Exception:  # noqa: BLE001 — prompt store down: fall back deterministically
        return _fallback_contract(project)

    prompt = (
        template
        .replace("[[REQUIREMENTS]]", requirements)
        .replace("[[FLOW]]", flow)
        .replace("[[DOCUMENTS]]", documents)
    )
    try:
        result = provider.generate_text(prompt)
    except Exception as error:  # noqa: BLE001
        logger.warning("contract synthesis model call raised: %s", error)
        return _fallback_contract(project)
    if not result.success:
        logger.warning("contract synthesis model returned failure: %s", result.error)
        return _fallback_contract(project)
    parsed = _extract_json(result.text)
    if not parsed or not (parsed.get("openapi") or parsed.get("api_summary")):
        logger.warning("contract synthesis output unusable; using fallback")
        return _fallback_contract(project)
    # Normalize shape so downstream consumers can rely on the keys existing.
    parsed.setdefault("openapi", {})
    parsed.setdefault("api_summary", "")
    parsed.setdefault("tech_stack", {})
    parsed.setdefault("middleware", {"datastores": [], "cache": None, "env": []})
    return parsed


def _seed_shared_ledger(project: CodeProject) -> dict:
    """Build the consensus ledger seed the three runs branch from.

    Reuses the most recent full-generation run's ledger (the established
    consensus), falling back to a fresh seed from the project inputs.
    """
    prior = (
        AgentRun.query.filter_by(resource_id=project.id, workflow="code_full_generation")
        .order_by(AgentRun.created_at.desc())
        .first()
    )
    ledger = ContextLedger.load(prior.get_context_ledger() if prior else None)
    if ledger.is_empty():
        ledger = seed_from_inputs(
            project.requirement_input, project.title, project.get_selected_style_ids()
        )
    return ledger.to_dict()


def render_contract_for_prompt(contract: dict, *, max_chars: int = 9000) -> str:
    """Render a compact contract block injected into the BE / FE build prompts.

    Both services must implement / consume the SAME endpoints, so this is the
    authoritative API surface. Prefers the structured OpenAPI paths; falls back
    to the api_summary markdown.
    """
    if not contract:
        return ""
    lines: list[str] = ["## 共享 API 契约(前后端必须严格一致 — 后端实现、前端消费)"]
    ts = contract.get("tech_stack") or {}
    if ts.get("language") or ts.get("framework"):
        lines.append(f"- 后端技术栈:{ts.get('language', '')} / {ts.get('framework', '')}")
    openapi = contract.get("openapi") or {}
    paths = openapi.get("paths") if isinstance(openapi, dict) else None
    if isinstance(paths, dict) and paths:
        lines.append("### 端点清单(method path — 摘要)")
        for path, ops in list(paths.items())[:60]:
            if not isinstance(ops, dict):
                continue
            for method, op in ops.items():
                if method.lower() not in ("get", "post", "put", "patch", "delete"):
                    continue
                summary = (op.get("summary") or op.get("operationId") or "") if isinstance(op, dict) else ""
                lines.append(f"- {method.upper()} {path} — {summary}".rstrip(" —"))
        # Inline the full OpenAPI JSON too (capped) so the agent has exact schemas.
        try:
            blob = json.dumps(openapi, ensure_ascii=False)
            lines.append("### OpenAPI 文档(JSON,权威 — schema/参数/返回以此为准)")
            lines.append(blob[: max(1000, max_chars - len("\n".join(lines)))])
        except (TypeError, ValueError):
            pass
    else:
        summary = contract.get("api_summary") or ""
        if summary:
            lines.append(summary)
    block = "\n".join(lines)
    return block[:max_chars]


def get_ledger(project_id: str) -> Optional[CodeProjectLedger]:
    return CodeProjectLedger.query.filter_by(project_id=project_id).first()


def ensure_contract(
    project: CodeProject, user_id: str, team_id: Optional[str] = None, *, force: bool = False
) -> CodeProjectLedger:
    """Synthesize (idempotently) and persist the shared contract for a project.

    Returns the persisted ``CodeProjectLedger``. When a READY contract already
    exists and ``force`` is False, returns it unchanged. The three runs read the
    contract from this row, so it must be committed before they start.
    """
    row = get_ledger(project.id)
    if row is None:
        row = CodeProjectLedger(
            project_id=project.id, user_id=user_id, team_id=team_id,
            contract_status=ContractStatus.PENDING, version=0,
        )
        db.session.add(row)
        db.session.commit()

    if row.contract_status == ContractStatus.READY and not force:
        return row

    row.contract_status = ContractStatus.BUILDING
    row.error_message = None
    db.session.commit()

    try:
        contract = synthesize_contract(project)
        seed = _seed_shared_ledger(project)
        row.set_api_contract(
            {
                "openapi": contract.get("openapi") or {},
                "api_summary": contract.get("api_summary") or "",
                "tech_stack": contract.get("tech_stack") or {},
            }
        )
        row.set_middleware_manifest(contract.get("middleware") or {})
        row.set_shared_ledger(seed)
        row.version = (row.version or 0) + 1
        row.contract_status = ContractStatus.READY
        db.session.commit()
    except Exception as error:  # noqa: BLE001 — record failure, never crash the request
        logger.error("contract synthesis failed for project %s: %s", project.id, error, exc_info=True)
        db.session.rollback()
        row = get_ledger(project.id)
        if row:
            row.contract_status = ContractStatus.FAILED
            row.error_message = str(error)
            db.session.commit()
        raise
    return row
