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
        # No model → can't infer realtime needs; default to no WebSocket channel.
        "realtime": {"enabled": False, "transport": "websocket", "auth": "query_token", "channels": []},
        # No model → no machine-readable schema; the deploy reconcile no-ops on empty.
        "db_schema": {"tables": []},
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
    parsed.setdefault(
        "realtime", {"enabled": False, "transport": "websocket", "auth": "query_token", "channels": []}
    )
    # Machine-readable authoritative DB schema (tables → columns → SQL type). The
    # deploy-time reconcile reads this to ADD missing columns / WIDEN narrow string
    # columns on the live DB; an absent/empty db_schema simply means "no reconcile".
    parsed.setdefault("db_schema", {"tables": []})
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


# The single schema source-of-truth. Injected verbatim into BOTH the backend
# (ORM) and middleware (init.sql) build prompts via render_contract_for_prompt,
# so the two generators — which run independently but whose outputs meet on the
# SAME database at deploy time — converge on ONE table layout. Drift here is what
# crashes a self-migrating backend on boot (init.sql creates `roles(id SERIAL)`
# with no created_at; the ORM expects uuid + timestamps; create_all skips the
# existing table; the first query hits a missing column → startup dies → health
# check fails → rollback). Pinning id strategy + timestamps + verbatim names +
# the TEXT default for non-PK string columns removes the drift at the source.
#
# Authority order (deploy time): the backend ORM's self-migration is the SINGLE
# schema author — deploy no longer pre-applies init.sql by default (that raced the
# ORM and固化d narrow/缺列 drift). init.sql is only a RECOVERY fallback applied if
# the backend can't build its own schema on an empty DB. They still must stay
# column-for-column consistent, since in the fallback case both touch one DB.
_SCHEMA_CONVENTION = """## 数据库 Schema 一致性公约(后端 ORM 与中间件 init.sql 必须共同遵守 — 数据层唯一真相源)
部署时**后端 ORM 的启动自迁移(create_all / AutoMigrate / Hibernate ddl 等)是建表的唯一权威**;中间件 init.sql 仅作「后端无法在空库自迁移」时的部署期兜底(默认不预先应用)。但二者一旦都作用于同一个库,结构必须**逐表逐列完全一致**,否则后端会因列缺失/类型不符在启动或运行时崩溃 → 健康检查失败 / 接口 500 → 整次部署回滚或功能不可用。严格遵守:
- **主键统一用字符串/UUID**:SQL 用 `VARCHAR(36)`(或 `TEXT`)主键 + 应用层生成 uuid;ORM 用字符串类型 id。**禁止** `SERIAL`/`BIGSERIAL`/`AUTO_INCREMENT`/自增整数主键。
- **外键类型与被引用主键一致**(同为字符串/UUID),不得一边整数一边 uuid。
- **每张表都带时间戳**:`created_at`、`updated_at`,类型 `TIMESTAMPTZ NOT NULL DEFAULT now()`(ORM 侧对应 created_at/updated_at 字段,所有表一致)。
- **非主键字符串列一律用 `TEXT`(无长度上限)**:`title`/`name`/`url`/`description`/`content`/`备注`/正文/JSON 串等一切业务文本列,**禁止**下发偏小的 `VARCHAR(n)`(如 `VARCHAR(20/50/255)`)——运行期一旦写入超过该长度的值,Postgres 会抛 `22001 value too long` 让接口 500、功能不可用(这是「字段内容过长」类故障的根因)。ORM 侧对应用 `Text`/`String`(不带长度)/`@Column(columnDefinition="TEXT")` 等无界文本类型。**仅当**某列确有明确固定上限的短编码/状态枚举(如国家码、订单号前缀)且你确知上限时,才可用 `VARCHAR(n)` 且 `n` 取足够大(≥255)、两侧同长度。
- **表名/列名/类型逐字一致、不得缺列**:以本契约的 `db_schema` 与 `components.schemas` 数据模型为准,两侧不得各自改名(如 url ↔ source_url)、改类型(如 VARCHAR ↔ Integer)或增减列;契约 `db_schema`/数据模型里出现的**每一个字段都必须在对应表里建出对应列**(漏列会导致部署后查询报「字段不存在」、功能不可用)。
- init.sql 用 `CREATE TABLE IF NOT EXISTS`、种子数据用 `ON CONFLICT DO NOTHING`,以容忍「后端已自建表」并保持幂等。
- 契约 `db_schema` 未明确某字段类型时,按本公约取默认(字符串主键 + 非主键字符串列 `TEXT` + 上述时间戳);双方一致即可。"""


def render_contract_for_prompt(
    contract: dict, *, max_chars: int = 9000, include_db_schema: bool = True
) -> str:
    """Render a compact contract block injected into the BE / FE build prompts.

    Both services must implement / consume the SAME endpoints, so this is the
    authoritative API surface. Prefers the structured OpenAPI paths; falls back
    to the api_summary markdown. Always appends the binding schema convention
    (``_SCHEMA_CONVENTION``) AFTER the cap so the BE/MW generators can't drift on
    id type / timestamps / column names — it is never truncated away.

    ``include_db_schema`` renders the authoritative table/column layout (consumed
    by the BE ORM + MW init.sql + the deploy reconcile). The FRONTEND build doesn't
    touch the DB, so it passes ``False`` — otherwise the db_schema block is dead
    weight that crowds the FE's real concern (the API surface) out of the cap.
    """
    if not contract:
        return ""
    lines: list[str] = ["## 共享 API 契约(前后端必须严格一致 — 后端实现、前端消费)"]
    ts = contract.get("tech_stack") or {}
    if ts.get("language") or ts.get("framework"):
        lines.append(f"- 后端技术栈:{ts.get('language', '')} / {ts.get('framework', '')}")
    # Realtime channels go HIGH in the block (before the potentially-large OpenAPI
    # paths/blob) so neither this render's cap nor the FE's own re-cap truncates
    # them away. Only emitted when the contract actually declares WebSocket needs;
    # a pure request/response app's block stays exactly as before.
    realtime = contract.get("realtime") or {}
    if isinstance(realtime, dict) and realtime.get("enabled") and realtime.get("channels"):
        lines.append(
            "### 实时通道(WebSocket — 与 HTTP 共用 PORT;握手鉴权走 ?token=<JWT> 查询参数;"
            "消息统一帧 {type,data,ts})"
        )
        for ch in (realtime.get("channels") or [])[:20]:
            if not isinstance(ch, dict):
                continue
            lines.append(
                f"- WS {ch.get('path', '')} — {ch.get('summary', '')} "
                f"({ch.get('direction', '')})".rstrip(" ()")
            )
        try:
            lines.append("#### realtime(JSON,权威):" + json.dumps(realtime, ensure_ascii=False))
        except (TypeError, ValueError):
            pass
    # Authoritative DB schema (machine-readable): the table/column layout BOTH the
    # backend ORM and the middleware init.sql must reproduce column-for-column. Placed
    # HIGH (before the potentially-large OpenAPI blob) so neither this render's cap nor
    # a downstream re-cap truncates it — it is the source of truth for the deploy-time
    # schema reconcile. Non-PK string columns are TEXT here (no narrow VARCHAR).
    db_schema = contract.get("db_schema") or {}
    tables = db_schema.get("tables") if isinstance(db_schema, dict) else None
    if include_db_schema and isinstance(tables, list) and tables:
        lines.append(
            "### 数据库 Schema(权威 — 后端 ORM 与 init.sql 必须逐表逐列一致;"
            "非主键字符串列一律 TEXT,禁用偏小 VARCHAR;部署期会据此校准实库)"
        )
        for tbl in tables[:40]:
            if not isinstance(tbl, dict):
                continue
            col_strs: list[str] = []
            for col in (tbl.get("columns") or [])[:60]:
                if not isinstance(col, dict):
                    continue
                seg = f"{col.get('name', '')} {col.get('type', '')}".strip()
                flags = []
                if col.get("pk"):
                    flags.append("PK")
                if col.get("unique"):
                    flags.append("UNIQUE")
                if col.get("nullable") is False:
                    flags.append("NOT NULL")
                if flags:
                    seg += " " + " ".join(flags)
                col_strs.append(seg)
            lines.append(f"- 表 {tbl.get('name', '')}:" + "; ".join(col_strs))
        try:
            # Compact JSON dump too (bounded so it can't crowd the endpoint list /
            # OpenAPI blob out of the cap); the human-readable lines above are the
            # primary signal and are already compact.
            lines.append(
                "#### db_schema(JSON,权威 — 表/列/类型以此为准):"
                + json.dumps(db_schema, ensure_ascii=False)[:2000]
            )
        except (TypeError, ValueError):
            pass

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
    block = "\n".join(lines)[:max_chars]
    # Append the schema convention AFTER the cap so it is guaranteed present in
    # full (a large OpenAPI blob must never crowd the source-of-truth rules out).
    return f"{block}\n\n{_SCHEMA_CONVENTION}"


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
                # Realtime/WebSocket channels (default disabled) — the FE/BE build
                # prompts read this from the contract, so it must be persisted.
                "realtime": contract.get("realtime")
                or {"enabled": False, "transport": "websocket", "auth": "query_token", "channels": []},
                # Authoritative DB schema — the deploy-time schema reconcile reads this
                # to align the live DB (ADD missing columns / WIDEN narrow string cols).
                "db_schema": contract.get("db_schema") or {"tables": []},
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
