"""
Code project "remix canvas" model (n8n-style node graph).

A canvas is a long-lived, editable design asset attached to a CodeProject. It
stores a node/edge graph where existing stage products (requirements / flow /
documents / style / preview) become read-only *source* nodes that the user wires
into custom *agent* nodes (free prompt + role + model), *merge* nodes, and
*branch* nodes. Executing a canvas is a separate ``code_canvas_generation`` agent
run; the graph itself persists independently of any single run.

The node/edge JSON is the shared contract between the React Flow frontend and the
DAG executor. The backend only reads execution-relevant fields (id / type /
data.config / source / target / sourceHandle) and ignores pure-UI fields such as
``position``.
"""
import json
import uuid
from datetime import datetime

from backend.extensions import db


class CodeCanvasNodeType:
    """Canvas node kinds (kept in sync with the frontend node registry)."""

    SOURCE_DOC = "source_doc"  # read-only reference to a stage product
    AGENT = "agent"  # free-prompt LLM node (selectable role + model)
    MERGE = "merge"  # concatenate upstream outputs, no LLM call
    BRANCH = "branch"  # route downstream by a classified key

    ALL = (SOURCE_DOC, AGENT, MERGE, BRANCH)
    EXECUTABLE = (AGENT, MERGE, BRANCH)  # source_doc is pre-filled, not executed


class CodeCanvas(db.Model):
    """An editable node graph that remixes a CodeProject's stage products."""

    __tablename__ = "code_canvases"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(
        db.String(36), db.ForeignKey("code_projects.id"), nullable=False, index=True
    )
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    team_id = db.Column(db.String(36), db.ForeignKey("teams.id"), nullable=True, index=True)

    name = db.Column(db.String(200), nullable=False, default="未命名画布")

    # JSON payloads: nodes_raw -> Node[], edges_raw -> Edge[], viewport_raw -> {x,y,zoom}
    nodes_raw = db.Column(db.Text, nullable=True)
    edges_raw = db.Column(db.Text, nullable=True)
    viewport_raw = db.Column(db.Text, nullable=True)

    # Most recent code_canvas_generation run for this canvas (for re-opening replay).
    last_run_id = db.Column(db.String(36), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @staticmethod
    def _load_json(raw, default):
        """Parse a JSON column, falling back to ``default`` on missing/invalid data."""
        if not raw:
            return default
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return default

    def get_nodes(self) -> list[dict]:
        """Return the node list."""
        value = self._load_json(self.nodes_raw, [])
        return value if isinstance(value, list) else []

    def set_nodes(self, nodes) -> None:
        """Persist the node list."""
        self.nodes_raw = json.dumps(nodes or [], ensure_ascii=False)

    def get_edges(self) -> list[dict]:
        """Return the edge list."""
        value = self._load_json(self.edges_raw, [])
        return value if isinstance(value, list) else []

    def set_edges(self, edges) -> None:
        """Persist the edge list."""
        self.edges_raw = json.dumps(edges or [], ensure_ascii=False)

    def get_viewport(self) -> dict:
        """Return the saved canvas viewport (pan/zoom)."""
        value = self._load_json(self.viewport_raw, {})
        return value if isinstance(value, dict) else {}

    def set_viewport(self, viewport) -> None:
        """Persist the canvas viewport (None clears it)."""
        self.viewport_raw = (
            json.dumps(viewport, ensure_ascii=False) if viewport is not None else None
        )

    def to_dict(self, include_graph: bool = True) -> dict:
        """Convert to an API dictionary; graph is omitted from list views."""
        data = {
            "id": self.id,
            "project_id": self.project_id,
            "user_id": self.user_id,
            "team_id": self.team_id,
            "name": self.name,
            "last_run_id": self.last_run_id,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "updated_at": self.updated_at.isoformat() + "Z" if self.updated_at else None,
        }
        if include_graph:
            data["nodes"] = self.get_nodes()
            data["edges"] = self.get_edges()
            data["viewport"] = self.get_viewport()
        return data
