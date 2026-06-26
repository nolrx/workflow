"""
Typed workflow contracts — the governed layer on top of the remix canvas.

The existing canvas (``CodeCanvas`` + ``dag_engine.CanvasGraph`` + ``canvas_nodes``)
already provides freeform node-graph composition (source / agent / merge / branch).
This package adds the *typed* layer the freeform canvas lacks:

  * ``ports``          — a namespaced registry of port content-types, so edges
                         between typed nodes can be type-checked.
  * ``node_contract``  — ``NodeContract``: the reusable definition of a typed
                         node (its typed input/output ports, prompt pin, context
                         read/write scope, executor and pricing).
  * ``defaults``       — built-in contracts that turn the existing generation
                         stages (requirements / flow / … / deploy) into typed,
                         composable canvas nodes.
  * ``validate``       — ``validate_graph``: a typed-port validation pass layered
                         on ``CanvasGraph`` (it does NOT re-implement the DAG).

Pure Python (no Flask / DB import) so it stays unit-testable and reusable from
both the canvas executor and any future workflow surface.
"""
from backend.services.agent.contracts.freeze import freeze_stage_prompts
from backend.services.agent.contracts.node_contract import (
    CONTRACT_SCHEMA_VERSION,
    KNOWN_EXECUTORS,
    WIRED_EXECUTORS,
    NodeContract,
    Port,
    PromptRef,
    load_contract,
)
from backend.services.agent.contracts.ports import (
    PORT_SCHEMA_VERSION,
    REF_KINDS,
    PortType,
    is_registered,
    make_port_value,
    register_port_type,
    registered_keys,
)
from backend.services.agent.contracts.validate import (
    LEDGER_SECTIONS,
    validate_contract_resources,
    validate_graph,
)

__all__ = [
    "PORT_SCHEMA_VERSION",
    "REF_KINDS",
    "PortType",
    "register_port_type",
    "is_registered",
    "registered_keys",
    "make_port_value",
    "CONTRACT_SCHEMA_VERSION",
    "KNOWN_EXECUTORS",
    "WIRED_EXECUTORS",
    "NodeContract",
    "Port",
    "PromptRef",
    "load_contract",
    "LEDGER_SECTIONS",
    "validate_graph",
    "validate_contract_resources",
    "freeze_stage_prompts",
]
