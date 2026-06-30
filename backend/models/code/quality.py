"""
Generation-quality sample store (the eval framework's persistence layer, P0-B).

One row per quality observation, so generation quality becomes MEASURABLE over
time instead of only living in transient run events. Two kinds:

  * ``online`` — written at the end of a frontend/backend project run's
    verify->repair loop (the final ``Verification`` verdict + rubric scores +
    feature coverage + repair rounds + degradation). Powers the success-rate /
    mean-score trend endpoint.
  * ``eval`` — written by the offline eval harness (``scripts/eval_review.py``
    run with ``--persist``): a labeled fixture's verdict + whether the skeptical
    evaluator discriminated correctly. Lets a critic-prompt change be checked for
    judgment regressions over time.

JSON-in-Text + string-UUID PK + nullable team_id, matching the rest of the Code
domain. The table is wholly new, so ``db.create_all()`` owns it (no Alembic;
``schema_guard`` only backfills columns on pre-existing tables and skips new ones).
"""
import json
import uuid
from datetime import datetime

from backend.extensions import db


class QualitySampleKind:
    """Where a quality sample came from."""

    ONLINE = "online"  # a real frontend/backend generation run
    EVAL = "eval"      # an offline labeled fixture (regression guard for the critic)

    ALL = {ONLINE, EVAL}


class CodeQualitySample(db.Model):
    """One measured generation-quality observation (online run or offline eval)."""

    __tablename__ = "code_quality_samples"

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # project_id / run_id are NULL for offline eval fixtures.
    project_id = db.Column(db.String(36), nullable=True, index=True)
    run_id = db.Column(db.String(36), nullable=True, index=True)
    user_id = db.Column(db.String(36), nullable=True, index=True)
    team_id = db.Column(db.String(36), nullable=True, index=True)

    kind = db.Column(db.String(16), nullable=False, default=QualitySampleKind.ONLINE, index=True)
    lane = db.Column(db.String(16), nullable=False, default="frontend", index=True)  # frontend|backend

    # --- rubric verdict ---
    verdict = db.Column(db.String(16), nullable=True)        # PASS | CONCERNS | FAIL
    weighted_score = db.Column(db.Float, nullable=True)      # 0~5 overall (see _verify_support.RUBRIC_WEIGHTS)
    scores_raw = db.Column(db.Text, nullable=True)           # {design_quality, originality, craft, functionality}

    # --- acceptance coverage ---
    feature_passed = db.Column(db.Integer, nullable=True)
    feature_total = db.Column(db.Integer, nullable=True)

    # --- gate outcome ---
    blocking = db.Column(db.Boolean, nullable=True)          # final round still had an objective defect?
    block_reasons_raw = db.Column(db.Text, nullable=True)    # ["house_rule","runtime","review","threshold"]
    verify_rounds = db.Column(db.Integer, nullable=True)     # verify passes that ran (1 = no repair needed)
    degraded_reason = db.Column(db.String(48), nullable=True)

    # --- panel / provenance ---
    panel_n = db.Column(db.Integer, nullable=True)           # reviewers in the consensus panel
    panel_flagging = db.Column(db.Integer, nullable=True)    # how many flagged blocking
    prompt_version = db.Column(db.String(48), nullable=True)
    model_name = db.Column(db.String(96), nullable=True)

    # --- offline eval only ---
    fixture_name = db.Column(db.String(96), nullable=True)
    expected_block = db.Column(db.Boolean, nullable=True)
    correct = db.Column(db.Boolean, nullable=True)           # blocked == expected_block?

    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    # ---- JSON helpers --------------------------------------------------------
    @staticmethod
    def _load(raw, default):
        if not raw:
            return default
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return default

    def get_scores(self) -> dict:
        return self._load(self.scores_raw, {})

    def set_scores(self, data: dict | None) -> None:
        self.scores_raw = json.dumps(data or {}, ensure_ascii=False)

    def get_block_reasons(self) -> list:
        return self._load(self.block_reasons_raw, [])

    def set_block_reasons(self, data) -> None:
        self.block_reasons_raw = json.dumps(list(data or []), ensure_ascii=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "user_id": self.user_id,
            "team_id": self.team_id,
            "kind": self.kind,
            "lane": self.lane,
            "verdict": self.verdict,
            "weighted_score": self.weighted_score,
            "scores": self.get_scores(),
            "feature_passed": self.feature_passed,
            "feature_total": self.feature_total,
            "blocking": self.blocking,
            "block_reasons": self.get_block_reasons(),
            "verify_rounds": self.verify_rounds,
            "degraded_reason": self.degraded_reason,
            "panel_n": self.panel_n,
            "panel_flagging": self.panel_flagging,
            "prompt_version": self.prompt_version,
            "model_name": self.model_name,
            "fixture_name": self.fixture_name,
            "expected_block": self.expected_block,
            "correct": self.correct,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
        }
