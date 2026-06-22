"""
GitHub integration models (Code domain).

Each Code session (``CodeProject``) maps 1:1 to a GitHub repository created under
the org where the platform's GitHub App is installed. ``GitHubRepoLink`` records
that mapping (so repeated pushes target the same repo and accrue version history);
``GitHubPushLog`` is the append-only history of sync attempts.

Neither model stores any secret — the App private key lives in the environment,
and installation tokens are short-lived and never persisted.
"""
import uuid
from datetime import datetime

from backend.extensions import db


class GitHubPushStatus:
    """Sync attempt status constants."""

    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"


class GitHubRepoLink(db.Model):
    """1:1 mapping from a Code session to its GitHub repository."""

    __tablename__ = "github_repo_links"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(
        db.String(36), db.ForeignKey("code_projects.id"), nullable=False, index=True
    )
    user_id = db.Column(db.String(36), db.ForeignKey("users.id"), nullable=False, index=True)
    team_id = db.Column(db.String(36), db.ForeignKey("teams.id"), nullable=True, index=True)

    installation_id = db.Column(db.String(40), nullable=True)
    repo_owner = db.Column(db.String(120), nullable=False)
    repo_name = db.Column(db.String(120), nullable=False)
    repo_id = db.Column(db.String(40), nullable=True)
    default_branch = db.Column(db.String(80), nullable=False, default="main")
    html_url = db.Column(db.String(400), nullable=True)
    visibility = db.Column(db.String(20), nullable=False, default="private")

    # Denormalised latest-push summary for cheap status reads.
    last_commit_sha = db.Column(db.String(40), nullable=True)
    last_pushed_at = db.Column(db.DateTime, nullable=True)
    last_status = db.Column(db.String(20), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # One repo per session.
    __table_args__ = (db.UniqueConstraint("project_id", name="uq_github_repo_link_project"),)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "repo_owner": self.repo_owner,
            "repo_name": self.repo_name,
            "full_name": f"{self.repo_owner}/{self.repo_name}",
            "default_branch": self.default_branch,
            "html_url": self.html_url,
            "visibility": self.visibility,
            "last_commit_sha": self.last_commit_sha,
            "last_pushed_at": self.last_pushed_at.isoformat() + "Z"
            if self.last_pushed_at
            else None,
            "last_status": self.last_status,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "updated_at": self.updated_at.isoformat() + "Z" if self.updated_at else None,
        }


class GitHubPushLog(db.Model):
    """A single auto-sync attempt (one per completed generation run)."""

    __tablename__ = "github_push_logs"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(
        db.String(36), db.ForeignKey("code_projects.id"), nullable=False, index=True
    )
    repo_link_id = db.Column(
        db.String(36), db.ForeignKey("github_repo_links.id"), nullable=True, index=True
    )
    run_id = db.Column(db.String(36), nullable=True, index=True)

    status = db.Column(db.String(20), nullable=False, default=GitHubPushStatus.PENDING)
    branch = db.Column(db.String(80), nullable=True)
    commit_sha = db.Column(db.String(40), nullable=True)
    files_count = db.Column(db.Integer, nullable=True)
    message = db.Column(db.Text, nullable=True)
    error_message = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    finished_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "repo_link_id": self.repo_link_id,
            "run_id": self.run_id,
            "status": self.status,
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "files_count": self.files_count,
            "message": self.message,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "finished_at": self.finished_at.isoformat() + "Z" if self.finished_at else None,
        }
