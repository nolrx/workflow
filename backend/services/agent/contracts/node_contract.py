"""
Node contract — the reusable, typed definition of one canvas node.

A ``NodeContract`` is to a typed canvas node what a function signature is to a
call: it declares the node's typed input/output ports, which prompt it is pinned
to, which Session-Context-Ledger sections it may read/write, how it is billed,
and which executor actually runs it. Contracts are the "components" a user wires
together on the canvas; ``defaults.py`` ships the built-in ones (the existing
generation stages turned into typed nodes).

Storage mirrors ``prompt_store``: built-in contracts are bundled defaults and may
be overridden in Mongo (added in a later increment). This module is pure data +
validation helpers — no Flask, no DB.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from backend.services.agent.contracts import ports

CONTRACT_SCHEMA_VERSION = 1

# Executor ids a contract may name. The executor *registry* (the code that
# actually runs each one) lands in a later increment; validation only checks the
# name is known so a contract can't reference a non-existent runner.
KNOWN_EXECUTORS = {
    "stage_text",     # text-generation stage (prompt -> text provider -> doc)
    "stage_preview",  # UI preview image generation
    "container_fe",   # frontend project build (fe-agent container)
    "container_be",   # backend project build (be-agent container)
    "provision_mw",   # middleware schema / migration / seed artifacts
    "deploy",         # full-stack deploy (build / run / health / proxy)
    "analysis",       # structured-JSON analysis (e.g. iteration impact)
}

# Of the KNOWN executors, those the canvas loop can actually run TODAY. The rest
# are declared (so their stage nodes validate + place on the canvas) but fail at
# run time until their executor is wired in. Keep in sync with the dispatch in
# ``workflows/code_canvas_workflow.py``.
WIRED_EXECUTORS = {
    "stage_text",
    "stage_preview",
    "container_fe",
    "container_be",
    "provision_mw",
    "deploy",
}


@dataclass(frozen=True)
class Port:
    """One typed input or output port on a node."""

    name: str
    type: str  # a registered port type key (see ports.py)
    required: bool = False


@dataclass(frozen=True)
class PromptRef:
    """A pin to a prompt. ``version``/``hash`` are null while a graph is a draft
    (resolves to HEAD); a published graph freezes them to an exact version."""

    key: str
    version: int | None = None
    hash: str | None = None

    def to_dict(self) -> dict:
        return {"key": self.key, "version": self.version, "hash": self.hash}


@dataclass
class NodeContract:
    """The typed definition of one node type."""

    node_type: str
    role: str  # planner | generator | critic | publisher (aligns with AgentStep.role)
    inputs: list[Port] = field(default_factory=list)
    outputs: list[Port] = field(default_factory=list)
    context_reads: list[str] = field(default_factory=list)
    context_writes: list[str] = field(default_factory=list)
    prompt_ref: PromptRef | None = None
    review_gate: bool = False
    pricing_key: str = ""
    executor: str = ""
    version: int = 1

    # ---- lookups ------------------------------------------------------------
    def input(self, name: str | None) -> Port | None:
        return next((p for p in self.inputs if p.name == name), None)

    def output(self, name: str | None) -> Port | None:
        return next((p for p in self.outputs if p.name == name), None)

    def output_type(self, name: str | None) -> str | None:
        port = self.output(name)
        return port.type if port else None

    def required_inputs(self) -> list[Port]:
        return [p for p in self.inputs if p.required]

    # ---- identity -----------------------------------------------------------
    def spec_dict(self) -> dict:
        """Canonical, version-independent spec used for hashing + freezing."""
        return {
            "schema_version": CONTRACT_SCHEMA_VERSION,
            "node_type": self.node_type,
            "role": self.role,
            "inputs": [
                {"name": p.name, "type": p.type, "required": p.required} for p in self.inputs
            ],
            "outputs": [{"name": p.name, "type": p.type} for p in self.outputs],
            "context": {"reads": list(self.context_reads), "writes": list(self.context_writes)},
            "prompt_ref": self.prompt_ref.to_dict() if self.prompt_ref else None,
            "review_gate": self.review_gate,
            "pricing_key": self.pricing_key,
            "executor": self.executor,
        }

    def spec_hash(self) -> str:
        """Stable content hash of the spec (the frozen identity used by a
        published graph). Independent of ``version`` so a re-publish with the
        same spec is a no-op."""
        blob = json.dumps(self.spec_dict(), sort_keys=True, ensure_ascii=False)
        return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        data = self.spec_dict()
        data["version"] = self.version
        data["spec_hash"] = self.spec_hash()
        return data

    def to_catalog(self) -> dict:
        """Frontend-facing view for the canvas node palette / typed handles.

        ``executable`` flags whether the canvas loop can actually run this node
        yet — only ``stage_text`` is wired today, so the palette can grey out the
        container / preview / deploy stages until their executors land.
        """
        return {
            "node_type": self.node_type,
            "role": self.role,
            "review_gate": self.review_gate,
            "executable": self.executor in WIRED_EXECUTORS,
            "inputs": [
                {"name": p.name, "type": p.type, "required": p.required} for p in self.inputs
            ],
            "outputs": [{"name": p.name, "type": p.type} for p in self.outputs],
            "prompt_key": self.prompt_ref.key if self.prompt_ref else None,
        }


def _port(raw: dict) -> Port:
    return Port(
        name=str(raw.get("name") or ""),
        type=str(raw.get("type") or ""),
        required=bool(raw.get("required", False)),
    )


def load_contract(data: dict) -> NodeContract:
    """Tolerantly build a ``NodeContract`` from a persisted/seed dict.

    Missing keys fall back to empty/defaults so a partial or legacy document
    never crashes the loader (mirrors ``ContextLedger.load``).
    """
    data = data or {}
    context = data.get("context") or {}
    prompt = data.get("prompt_ref")
    prompt_ref = None
    if isinstance(prompt, dict) and prompt.get("key"):
        prompt_ref = PromptRef(
            key=str(prompt["key"]),
            version=prompt.get("version"),
            hash=prompt.get("hash"),
        )
    return NodeContract(
        node_type=str(data.get("node_type") or ""),
        role=str(data.get("role") or "generator"),
        inputs=[_port(p) for p in (data.get("inputs") or []) if isinstance(p, dict)],
        outputs=[_port(p) for p in (data.get("outputs") or []) if isinstance(p, dict)],
        context_reads=[str(s) for s in (context.get("reads") or [])],
        context_writes=[str(s) for s in (context.get("writes") or [])],
        prompt_ref=prompt_ref,
        review_gate=bool(data.get("review_gate", False)),
        pricing_key=str(data.get("pricing_key") or ""),
        executor=str(data.get("executor") or ""),
        version=int(data.get("version", 1) or 1),
    )


# Re-export for callers that only import this module.
__all__ = [
    "CONTRACT_SCHEMA_VERSION",
    "KNOWN_EXECUTORS",
    "WIRED_EXECUTORS",
    "Port",
    "PromptRef",
    "NodeContract",
    "load_contract",
    "ports",
]
