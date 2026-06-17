"""
Session Context Ledger — the evolving, validated "consensus" of one AgentRun.

The ledger is a compact, structured record of what has been *established* about a
project as a Code-domain workflow progresses: the one-line product定位, the
glossary / 术语口径, the tech-stack口径, key decisions, hard constraints and the
carried-forward 待确认问题. Each downstream step renders the ledger into a short
text block (prepended to that step's prompt) so later agents stay on-口径 instead
of re-interpreting whole documents from scratch.

Design rules:
- Pure Python (no Flask / DB import). The workflow owns persistence; this object
  is just (de)serialized via :meth:`to_dict` / :meth:`load`.
- Backward compatible: an empty / legacy ledger renders to ``""`` so injection is
  a no-op for runs created before this feature.
- ``render_for_prompt`` output is plain markdown with no single ``{`` / ``}`` so it
  is safe to pass as a ``str.format`` argument (arguments are not re-scanned) and
  as a ``str.replace`` value.

See docs/agent-context-ledger.md for the authoritative schema + lifecycle.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Default tech-stack口径 — mirrors the frontend workflow's single-file HTML
# delivery and the development-flow fallback's Flask backend assumption.
DEFAULT_FRONTEND_STACK = "Single-file HTML + inline CSS + browser-native JavaScript"
DEFAULT_BACKEND_STACK = "Flask (Python)"


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _clean_str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _dedupe_extend(base: list[str], extra: Optional[list]) -> list[str]:
    """Append normalized, non-empty strings from ``extra`` not already in ``base``."""
    if not extra:
        return base
    seen = {item.strip() for item in base if isinstance(item, str)}
    for raw in extra:
        text = _clean_str(raw)
        if text and text not in seen:
            base.append(text)
            seen.add(text)
    return base


class ContextLedger:
    """Mutable, in-memory consensus ledger for one agent run."""

    def __init__(self, data: dict):
        self._data = data

    # ---- construction --------------------------------------------------------
    @classmethod
    def empty(cls) -> "ContextLedger":
        return cls(
            {
                "schema_version": SCHEMA_VERSION,
                "project": {
                    "title": "",
                    "one_liner": "",
                    "target_users": [],
                    "scope_in": [],
                    "scope_out": [],
                },
                "glossary": [],
                "tech_stack": {
                    "frontend": "",
                    "backend": "",
                    "data": "",
                    "constraints": [],
                },
                "decisions": [],
                "constraints": [],
                "open_questions": [],
                "provenance": [],
            }
        )

    @classmethod
    def load(cls, data: Optional[dict]) -> "ContextLedger":
        """Tolerantly load a persisted ledger; missing keys fall back to empty().

        An unknown ``schema_version`` is treated as empty (logged) so a future
        schema bump can never crash an in-flight or replayed run.
        """
        ledger = cls.empty()
        if not data or not isinstance(data, dict):
            return ledger
        version = data.get("schema_version")
        if version not in (None, SCHEMA_VERSION):
            logger.warning(
                "Unknown context ledger schema_version=%s; treating as empty.", version
            )
            return ledger
        base = ledger._data
        project = data.get("project")
        if isinstance(project, dict):
            base["project"].update({k: project.get(k, base["project"][k]) for k in base["project"]})
        tech = data.get("tech_stack")
        if isinstance(tech, dict):
            base["tech_stack"].update({k: tech.get(k, base["tech_stack"][k]) for k in base["tech_stack"]})
        for key in ("glossary", "decisions", "constraints", "open_questions", "provenance"):
            value = data.get(key)
            if isinstance(value, list):
                base[key] = value
        return ledger

    # ---- serialization -------------------------------------------------------
    def to_dict(self) -> dict:
        return self._data

    @property
    def project(self) -> dict:
        return self._data["project"]

    @property
    def tech_stack(self) -> dict:
        return self._data["tech_stack"]

    def is_empty(self) -> bool:
        p = self._data["project"]
        return not (
            p.get("one_liner")
            or p.get("target_users")
            or p.get("scope_in")
            or self._data["glossary"]
            or self._data["decisions"]
            or self._data["constraints"]
            or any(self._data["tech_stack"].get(k) for k in ("frontend", "backend", "data"))
        )

    # ---- mutation ------------------------------------------------------------
    def merge(
        self,
        *,
        project: Optional[dict] = None,
        glossary_add: Optional[list] = None,
        tech_stack: Optional[dict] = None,
        decisions_add: Optional[list] = None,
        constraints_add: Optional[list] = None,
        open_questions: Optional[list] = None,
        provenance_entry: Optional[dict] = None,
    ) -> "ContextLedger":
        """Idempotently fold newly-established facts into the ledger.

        Dedup rules: glossary by lowercased ``term`` (latest definition wins),
        decisions by ``id``, constraints / scope / questions by normalized string.
        Empty / blank inputs are ignored so a no-op merge never erases prior口径.
        """
        if project:
            tgt = self._data["project"]
            for key in ("title", "one_liner"):
                val = _clean_str(project.get(key))
                if val:
                    tgt[key] = val
            for key in ("target_users", "scope_in", "scope_out"):
                if project.get(key):
                    _dedupe_extend(tgt[key], project[key])

        if glossary_add:
            self._merge_glossary(glossary_add)

        if tech_stack:
            tgt = self._data["tech_stack"]
            for key in ("frontend", "backend", "data"):
                val = _clean_str(tech_stack.get(key))
                if val:
                    tgt[key] = val
            if tech_stack.get("constraints"):
                _dedupe_extend(tgt["constraints"], tech_stack["constraints"])

        if decisions_add:
            self._merge_decisions(decisions_add)

        if constraints_add:
            _dedupe_extend(self._data["constraints"], constraints_add)

        if open_questions is not None:
            _dedupe_extend(self._data["open_questions"], open_questions)

        if provenance_entry:
            entry = dict(provenance_entry)
            entry.setdefault("at", _now_iso())
            self._data["provenance"].append(entry)

        return self

    def _merge_glossary(self, items: list) -> None:
        index = {
            _clean_str(g.get("term")).lower(): g
            for g in self._data["glossary"]
            if isinstance(g, dict)
        }
        for raw in items:
            if not isinstance(raw, dict):
                continue
            term = _clean_str(raw.get("term"))
            definition = _clean_str(raw.get("definition"))
            if not term or not definition:
                continue
            key = term.lower()
            entry = {
                "term": term,
                "definition": definition,
                "source_step": _clean_str(raw.get("source_step")),
            }
            if key in index:
                index[key].update(entry)
            else:
                index[key] = entry
                self._data["glossary"].append(entry)

    def _merge_decisions(self, items: list) -> None:
        index = {
            _clean_str(d.get("id")): d
            for d in self._data["decisions"]
            if isinstance(d, dict)
        }
        for raw in items:
            if not isinstance(raw, dict):
                continue
            did = _clean_str(raw.get("id"))
            statement = _clean_str(raw.get("statement"))
            if not did or not statement:
                continue
            entry = {
                "id": did,
                "statement": statement,
                "rationale": _clean_str(raw.get("rationale")),
                "source_step": _clean_str(raw.get("source_step")),
            }
            if did in index:
                index[did].update(entry)
            else:
                index[did] = entry
                self._data["decisions"].append(entry)

    # ---- rendering -----------------------------------------------------------
    def render_for_prompt(self, *, max_chars: int = 1800) -> str:
        """Render the compact consensus block prepended to downstream prompts.

        Returns ``""`` for an effectively empty ledger so injection is a no-op for
        legacy / freshly-seeded runs. Sections are dropped from the bottom up if
        the block would exceed ``max_chars``, always keeping the header.
        """
        if self.is_empty():
            return ""

        p = self._data["project"]
        ts = self._data["tech_stack"]
        sections: list[str] = ["## 项目共识（内部上下文，请严格遵循，勿改述、勿引入与此冲突的设定）"]

        consensus: list[str] = []
        if p.get("one_liner"):
            consensus.append(f"- 一句话定位: {p['one_liner']}")
        if p.get("target_users"):
            consensus.append(f"- 目标用户: {', '.join(p['target_users'])}")
        if p.get("scope_in"):
            consensus.append(f"- 范围内: {', '.join(p['scope_in'])}")
        if p.get("scope_out"):
            consensus.append(f"- 明确不做: {', '.join(p['scope_out'])}")
        if consensus:
            sections.append("\n".join(consensus))

        if self._data["glossary"]:
            lines = ["### 术语口径"] + [
                f"- {g['term']}: {g['definition']}" for g in self._data["glossary"]
            ]
            sections.append("\n".join(lines))

        if any(ts.get(k) for k in ("frontend", "backend", "data")) or ts.get("constraints"):
            lines = ["### 技术栈口径"]
            if ts.get("frontend"):
                lines.append(f"- 前端: {ts['frontend']}")
            if ts.get("backend"):
                lines.append(f"- 后端: {ts['backend']}")
            if ts.get("data"):
                lines.append(f"- 数据: {ts['data']}")
            for c in ts.get("constraints", []):
                lines.append(f"- 约束: {c}")
            sections.append("\n".join(lines))

        if self._data["decisions"]:
            lines = ["### 关键决策"]
            for d in self._data["decisions"]:
                because = f"（因为 {d['rationale']}）" if d.get("rationale") else ""
                lines.append(f"- [{d['id']}] {d['statement']}{because}")
            sections.append("\n".join(lines))

        if self._data["constraints"]:
            lines = ["### 全局约束"] + [f"- {c}" for c in self._data["constraints"]]
            sections.append("\n".join(lines))

        if self._data["open_questions"]:
            lines = ["### 待确认问题（保持一致，勿擅自当成已定结论）"] + [
                f"- {q}" for q in self._data["open_questions"]
            ]
            sections.append("\n".join(lines))

        # Trim from the bottom up (header always kept) to respect max_chars.
        while len(sections) > 1 and len("\n\n".join(sections)) > max_chars:
            sections.pop()
        return "\n\n".join(sections)

    def fingerprint(self) -> dict:
        """Small canonical口径 dict used as the AI gate's established baseline."""
        p = self._data["project"]
        return {
            "one_liner": p.get("one_liner", ""),
            "scope_in": list(p.get("scope_in", [])),
            "scope_out": list(p.get("scope_out", [])),
            "tech_stack": {
                "frontend": self._data["tech_stack"].get("frontend", ""),
                "backend": self._data["tech_stack"].get("backend", ""),
                "data": self._data["tech_stack"].get("data", ""),
            },
            "glossary": {
                g["term"]: g["definition"][:120] for g in self._data["glossary"]
            },
            "decisions": [d["statement"] for d in self._data["decisions"]],
        }


def seed_from_inputs(requirement: str, title: str, style_ids: Optional[list] = None) -> ContextLedger:
    """Build the initial ledger at the planner step from the run inputs."""
    ledger = ContextLedger.empty()
    requirement = _clean_str(requirement)
    ledger.merge(
        project={
            "title": _clean_str(title),
            "one_liner": requirement[:200],
        },
        tech_stack={
            "frontend": DEFAULT_FRONTEND_STACK,
            "backend": DEFAULT_BACKEND_STACK,
        },
        provenance_entry={
            "step": "planner",
            "agent_key": "planner",
            "fields_touched": ["project.title", "project.one_liner", "tech_stack"],
        },
    )
    if style_ids:
        ledger.merge(
            tech_stack={"constraints": [f"UI 风格: {', '.join(str(s) for s in style_ids)}"]},
        )
    return ledger
