"""
Figma design attached to a Code project.

One design per project (UPSERT on re-import). It holds, per top-level Figma
frame, a compact Design IR plus the on-disk filename of that frame's rendered
PNG (stored under the upload root, see ``services/code/figma/storage.py`` — the
PNG bytes are NOT inlined here). The multi-file project generation reads this to
feed the design (render images + IR) into the containerized build.
"""
import json
import uuid
from datetime import datetime

from backend.extensions import db


class CodeFigmaDesign(db.Model):
    """A Figma design (all top-level frames) attached to one Code project."""

    __tablename__ = "code_figma_designs"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(
        db.String(36), db.ForeignKey("code_projects.id"), nullable=False, index=True
    )
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    team_id = db.Column(db.String(36), db.ForeignKey("teams.id"), nullable=True, index=True)

    file_key = db.Column(db.String(120), nullable=False)
    file_name = db.Column(db.String(300), nullable=True)
    source_url = db.Column(db.Text, nullable=True)

    # JSON list: [{node_id, name, order, width, height, ir(compact dict), render_filename}]
    frames_raw = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # One attached design per project (re-import replaces it).
    __table_args__ = (db.UniqueConstraint("project_id", name="uq_figma_design_project"),)

    def get_frames(self) -> list[dict]:
        """Return the full frame list (including each frame's compact IR)."""
        if not self.frames_raw:
            return []
        try:
            value = json.loads(self.frames_raw)
            return value if isinstance(value, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    def set_frames(self, frames: list[dict]) -> None:
        """Persist the frame list."""
        self.frames_raw = json.dumps(frames or [], ensure_ascii=False)

    def to_dict(self, include_frames: bool = True) -> dict:
        """API view. Frames are returned WITHOUT their (large) IR payloads."""
        data = {
            "id": self.id,
            "project_id": self.project_id,
            "file_key": self.file_key,
            "file_name": self.file_name,
            "source_url": self.source_url,
            "count": len(self.get_frames()),
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "updated_at": self.updated_at.isoformat() + "Z" if self.updated_at else None,
        }
        if include_frames:
            data["frames"] = [
                {
                    "node_id": f.get("node_id"),
                    "name": f.get("name"),
                    "order": f.get("order"),
                    "width": f.get("width"),
                    "height": f.get("height"),
                }
                for f in self.get_frames()
            ]
        return data
