"""
Generic DAG engine for the remix canvas.

Business-agnostic graph plumbing: it normalizes the persisted node/edge JSON,
detects cycles, produces a topological execution order, and exposes ordered
incoming/outgoing edges. The actual node execution (LLM calls, merging,
branching) lives in ``canvas_nodes.py``; the orchestration loop that walks this
graph and prunes pruned-branch subgraphs lives in
``workflows/code_canvas_workflow.py``.
"""
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CanvasNodeView:
    """Normalized view of one persisted canvas node."""

    id: str
    type: str
    label: str
    config: dict


@dataclass
class CanvasEdgeView:
    """Normalized view of one persisted canvas edge."""

    id: str
    source: str
    target: str
    source_handle: Optional[str]
    target_handle: Optional[str]
    order: int


@dataclass
class NodeResult:
    """What a node executor returns to the orchestration loop."""

    output_text: str = ""
    # For branch nodes: the set of source-handle keys that stay active. None means
    # "all outgoing edges active" (the default for non-branch nodes).
    active_handles: Optional[set] = None
    output_summary: str = ""
    reasoning_summary: str = ""
    self_check: str = ""
    extra: dict = field(default_factory=dict)


class CanvasGraph:
    """Parsed, validated canvas graph ready for topological execution."""

    def __init__(self, nodes: list[dict], edges: list[dict]):
        self.nodes: dict[str, CanvasNodeView] = {}
        for raw in nodes or []:
            nid = raw.get("id")
            if not nid:
                continue
            data = raw.get("data") or {}
            self.nodes[nid] = CanvasNodeView(
                id=nid,
                type=raw.get("type") or "agent",
                label=(data.get("label") or nid),
                config=(data.get("config") or {}),
            )

        self.edges: list[CanvasEdgeView] = []
        for index, raw in enumerate(edges or []):
            src, tgt = raw.get("source"), raw.get("target")
            if src not in self.nodes or tgt not in self.nodes:
                continue  # dangling edge — ignore
            edge_data = raw.get("data") or {}
            order = edge_data.get("order")
            self.edges.append(
                CanvasEdgeView(
                    id=raw.get("id") or f"e{index}",
                    source=src,
                    target=tgt,
                    source_handle=raw.get("sourceHandle"),
                    target_handle=raw.get("targetHandle"),
                    order=order if isinstance(order, int) else index,
                )
            )

        self._incoming: dict[str, list[CanvasEdgeView]] = defaultdict(list)
        self._outgoing: dict[str, list[CanvasEdgeView]] = defaultdict(list)
        for edge in self.edges:
            self._incoming[edge.target].append(edge)
            self._outgoing[edge.source].append(edge)
        for bucket in (self._incoming, self._outgoing):
            for key in bucket:
                bucket[key].sort(key=lambda e: e.order)

    def incoming(self, node_id: str) -> list[CanvasEdgeView]:
        """Incoming edges for a node, ordered by edge ``order`` (merge order)."""
        return self._incoming.get(node_id, [])

    def outgoing(self, node_id: str) -> list[CanvasEdgeView]:
        """Outgoing edges for a node, ordered by edge ``order``."""
        return self._outgoing.get(node_id, [])

    def topo_order(self) -> list[str]:
        """Kahn topological sort; raises ValueError on a cycle.

        Insertion order of the nodes is preserved among ready nodes so execution
        is deterministic for a given graph.
        """
        indeg = {nid: 0 for nid in self.nodes}
        for edge in self.edges:
            indeg[edge.target] += 1
        queue = deque(nid for nid in self.nodes if indeg[nid] == 0)
        order: list[str] = []
        while queue:
            nid = queue.popleft()
            order.append(nid)
            for edge in self.outgoing(nid):
                indeg[edge.target] -= 1
                if indeg[edge.target] == 0:
                    queue.append(edge.target)
        if len(order) != len(self.nodes):
            raise ValueError("画布存在环，无法执行")
        return order
