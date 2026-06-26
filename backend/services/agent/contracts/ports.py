"""
Port type registry for typed workflow nodes.

A *port type* is the logical content-type that flows along a canvas edge (a
requirements doc, a UI preview, an API contract, …). Node contracts declare
typed input/output ports; an edge between two typed nodes is valid only when the
upstream output port's type equals the downstream input port's type.

Types are **namespaced** (``"<domain>:<name>"``) and live in a registry rather
than a central closed enum, so a new domain registers its own port types without
editing shared code. The engine only ever asks "is this type registered?" and
"are these two type strings equal?" — typed composition is a control-flow concern,
not a semantic-search one (no embeddings involved).

Pure Python: no Flask, no DB.
"""
from dataclasses import dataclass

PORT_SCHEMA_VERSION = 1

# How a port *value* points at its content at runtime. The value carried on an
# edge is always a reference ("how to fetch it again"), never inline bytes, so
# graphs stay small and replay-friendly. (Executors resolve these later.)
REF_KINDS = {
    "code_document",     # ref_id = CodeDocument.id
    "agent_artifact",    # ref_id = AgentArtifact.id
    "code_ledger_field",  # ref_id = CodeProjectLedger.id + field name
    "code_deployment",   # ref_id = CodeDeployment.id
    "inline_json",       # value embedded directly (small structured payloads)
    "inline_text",       # value embedded directly (free text)
}


@dataclass(frozen=True)
class PortType:
    """One registered port content-type."""

    key: str
    ref_kinds: frozenset
    describe: str


_REGISTRY: dict[str, PortType] = {}


def register_port_type(key: str, *, ref_kinds: set, describe: str) -> None:
    """Register a namespaced port type. Re-registering the same key overwrites it.

    Raises ``ValueError`` for a non-namespaced key or an unknown ref_kind so a
    typo fails loudly at import time rather than silently producing an
    un-checkable port.
    """
    if ":" not in key:
        raise ValueError(f"端口类型 key 必须带命名空间 '<domain>:<name>':{key}")
    unknown = set(ref_kinds) - REF_KINDS
    if unknown:
        raise ValueError(f"{key} 含未知 ref_kinds:{sorted(unknown)}")
    if not ref_kinds:
        raise ValueError(f"{key} 必须声明至少一个 ref_kind")
    _REGISTRY[key] = PortType(key=key, ref_kinds=frozenset(ref_kinds), describe=describe)


def make_port_value(
    type_key: str,
    ref_kind: str,
    *,
    ref_id: str | None = None,
    value=None,
    field: str | None = None,
    produced_by: str | None = None,
) -> dict:
    """Build a typed PortValue — a *reference* (id), not bytes — carried on an edge.

    See ``docs/composable-workflow-schema.md`` §3.3. ``ref_id`` points at the
    persisted product (a CodeDocument / AgentArtifact id); ``value`` is only used
    for the inline kinds. Kept small + JSON-serializable so it flows through the
    canvas run and is replay-recoverable from the product it names.
    """
    return {
        "type": type_key,
        "ref_kind": ref_kind,
        "ref_id": ref_id,
        "field": field,
        "value": value,
        "produced_by": produced_by,
        "port_schema_version": PORT_SCHEMA_VERSION,
    }


def is_registered(key: str) -> bool:
    return key in _REGISTRY


def get_port_type(key: str) -> PortType | None:
    return _REGISTRY.get(key)


def registered_keys() -> list[str]:
    return sorted(_REGISTRY)


def _register_builtin_port_types() -> None:
    """Core + Code-domain port types (policy, not engine)."""
    # Cross-cutting.
    register_port_type("core:context_ledger", ref_kinds={"inline_json"}, describe="横切共识账本")
    register_port_type("core:user_text", ref_kinds={"inline_text"}, describe="用户指令 / run 输入")
    # Code generation stage products.
    register_port_type("code:requirements_doc", ref_kinds={"code_document"}, describe="需求文档")
    register_port_type("code:development_flow", ref_kinds={"code_document"}, describe="开发流程")
    register_port_type("code:document_set", ref_kinds={"code_document"}, describe="拆分文档集")
    register_port_type("code:style_doc", ref_kinds={"code_document"}, describe="风格文档")
    register_port_type("code:ui_preview", ref_kinds={"agent_artifact"}, describe="UI 预览图")
    register_port_type(
        "code:api_contract",
        ref_kinds={"code_ledger_field", "agent_artifact"},
        describe="共享 OpenAPI 契约",
    )
    register_port_type(
        "code:frontend_project", ref_kinds={"agent_artifact"}, describe="前端工程产物"
    )
    register_port_type(
        "code:backend_project", ref_kinds={"agent_artifact"}, describe="后端工程产物"
    )
    register_port_type(
        "code:middleware_manifest",
        ref_kinds={"code_ledger_field", "agent_artifact"},
        describe="中间件清单",
    )
    register_port_type(
        "code:asset_manifest", ref_kinds={"agent_artifact"}, describe="图形资源 manifest"
    )
    register_port_type("code:deployment", ref_kinds={"code_deployment"}, describe="部署登记")


_register_builtin_port_types()
