"""
Typed-port validation for a canvas graph.

This does NOT re-implement the DAG — it reuses ``dag_engine.CanvasGraph`` for
parsing, cycle detection and topological order, and layers a typed check on top:

  * a node is *typed* when its ``config.contract_key`` resolves to a NodeContract;
  * an edge between two typed nodes is valid only when the upstream output port's
    type equals the downstream input port's type (both registered);
  * every required input of a typed node must have an incoming edge;
  * a typed node's declared port types / ledger sections must be valid.

Typed and untyped (freeform agent / merge / branch / source) nodes coexist: an
edge that touches an untyped node is simply not type-checked (the freeform canvas
keeps working). The check is fully deterministic — typed composition is a
control-flow concern, not a semantic-search one.

``validate_graph`` is pure (ports registry + ledger sections only) so it is
unit-testable offline and cheap enough to run on every canvas edit. Resource
existence (pricing key / executor / prompt) is a separate, lazily-importing
``validate_contract_resources`` so the hot path stays import-light.
"""
from collections.abc import Callable

from backend.services.agent.context_ledger import ContextLedger
from backend.services.agent.contracts import ports
from backend.services.agent.contracts.node_contract import KNOWN_EXECUTORS, NodeContract

# Valid Session-Context-Ledger section names a contract may read/write, derived
# from the ledger's own shape so the two never drift.
LEDGER_SECTIONS = set(ContextLedger.empty().to_dict()) - {"schema_version"}

ContractResolver = Callable[[str], NodeContract | None]


def _typed_nodes(graph, resolve_contract: ContractResolver) -> tuple[dict, list[str]]:
    """Map node id -> contract for every typed node; collect unknown-contract errors."""
    contracts: dict[str, NodeContract] = {}
    errors: list[str] = []
    for nid, node in graph.nodes.items():
        key = (node.config or {}).get("contract_key")
        if not key:
            continue  # untyped freeform node — not type-checked
        contract = resolve_contract(key)
        if contract is None:
            errors.append(f"节点「{node.label}」引用了未知契约:{key}")
        else:
            contracts[nid] = contract
    return contracts, errors


def _check_contract_ports(node_label: str, contract: NodeContract) -> list[str]:
    errors: list[str] = []
    for port in (*contract.inputs, *contract.outputs):
        if not ports.is_registered(port.type):
            errors.append(f"节点「{node_label}」端口 {port.name} 的类型未注册:{port.type}")
    for section in (*contract.context_reads, *contract.context_writes):
        if section not in LEDGER_SECTIONS:
            errors.append(f"节点「{node_label}」上下文段名非法:{section}")
    return errors


def _check_edges(graph, contracts: dict) -> list[str]:
    """Type-check every edge whose BOTH endpoints are typed."""
    errors: list[str] = []
    for edge in graph.edges:
        src, tgt = contracts.get(edge.source), contracts.get(edge.target)
        if not (src and tgt):
            continue  # one end is freeform — leave untyped
        out_type = src.output_type(edge.source_handle)
        in_port = tgt.input(edge.target_handle)
        s_label, t_label = graph.nodes[edge.source].label, graph.nodes[edge.target].label
        if out_type is None:
            errors.append(f"「{s_label}」没有输出端口 {edge.source_handle}")
        elif in_port is None:
            errors.append(f"「{t_label}」没有输入端口 {edge.target_handle}")
        elif out_type != in_port.type:
            errors.append(
                f"端口类型不匹配:「{s_label}」.{edge.source_handle}({out_type}) → "
                f"「{t_label}」.{edge.target_handle}({in_port.type})"
            )
    return errors


def _check_required_inputs(graph, contracts: dict) -> list[str]:
    errors: list[str] = []
    for nid, contract in contracts.items():
        connected = {e.target_handle for e in graph.incoming(nid)}
        for port in contract.required_inputs():
            if port.name not in connected:
                errors.append(f"节点「{graph.nodes[nid].label}」必填输入未连接:{port.name}")
    return errors


def validate_graph(graph, resolve_contract: ContractResolver) -> list[str]:
    """Return a list of human-readable validation errors (empty == valid).

    ``graph`` is a ``dag_engine.CanvasGraph``; ``resolve_contract`` maps a
    ``contract_key`` to a ``NodeContract`` (e.g. ``get_default_contract``).
    """
    errors: list[str] = []

    # 1. Topology / cycle — delegate to the existing engine.
    try:
        graph.topo_order()
    except ValueError as exc:
        errors.append(str(exc))

    # 2. Resolve typed nodes.
    contracts, unknown = _typed_nodes(graph, resolve_contract)
    errors.extend(unknown)

    # 3. Per-contract static validity.
    for nid, contract in contracts.items():
        errors.extend(_check_contract_ports(graph.nodes[nid].label, contract))

    # 4. Edge type matching + required inputs.
    errors.extend(_check_edges(graph, contracts))
    errors.extend(_check_required_inputs(graph, contracts))

    return errors


def validate_contract_resources(contract: NodeContract) -> list[str]:
    """Check a contract references resources that actually exist.

    Separate from ``validate_graph`` because it lazily imports pricing / prompts
    (kept out of the per-edit hot path). Run it when seeding / saving contracts.
    """
    errors: list[str] = []

    if contract.executor not in KNOWN_EXECUTORS:
        errors.append(f"契约 {contract.node_type} 的 executor 未知:{contract.executor}")

    if contract.pricing_key:
        from backend.services import pricing

        if not isinstance(getattr(pricing, contract.pricing_key, None), int):
            errors.append(f"契约 {contract.node_type} 的 pricing_key 不存在:{contract.pricing_key}")

    if contract.prompt_ref:
        from backend.services.prompts import defaults as prompt_defaults

        if prompt_defaults.get_default_content(contract.prompt_ref.key) is None:
            errors.append(
                f"契约 {contract.node_type} 的 prompt 不存在:{contract.prompt_ref.key}"
            )

    return errors
