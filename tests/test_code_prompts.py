"""Pytest gate: Code-domain prompt templates stay wiring-compatible.

Wraps ``scripts/validate_code_prompts.py`` so any future prompt edit that breaks
a placeholder, brace-escaping (``.format`` vs ``[[KEY]]`` fill), JSON output
contract, ledger-compat section header, or operational token fails CI. This is
the BMAD "definition of done" for the Code-domain prompts: the pipeline plumbing
can never silently break on a prompt change again.
"""
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VALIDATOR = REPO / "scripts" / "validate_code_prompts.py"


def _load_validator():
    spec = importlib.util.spec_from_file_location("validate_code_prompts", VALIDATOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_all_code_prompts_valid():
    """Every managed prompt passes the deterministic contract checks."""
    mod = _load_validator()
    failures: dict[str, list[str]] = {}
    for name, spec in mod.MANIFEST.items():
        path = mod.PROMPT_DIR / name
        assert path.is_file(), f"managed prompt file missing on disk: {name}"
        errs = mod.validate_one(name, spec, path.read_text(encoding="utf-8"))
        if errs:
            failures[name] = errs
    assert not failures, f"prompt contract violations:\n{failures}"


def test_manifest_files_all_exist():
    """No manifest entry points at a deleted prompt (catches stale manifest)."""
    mod = _load_validator()
    missing = [name for name in mod.MANIFEST if not (mod.PROMPT_DIR / name).is_file()]
    assert not missing, f"manifest references missing prompt files: {missing}"


def test_cross_prompt_semantics():
    """Cross-file semantic invariants hold (login token at data.token, frontend
    allows same-origin API, critic is fed its anchor sources). These catch
    contradictions BETWEEN prompts that per-file token checks pass right over."""
    mod = _load_validator()
    errs = mod.cross_prompt_checks(mod._load_all_texts())
    assert not errs, "cross-prompt semantic violations:\n" + "\n".join(errs)


def test_structural_sections():
    """Fixed-section prompts list every `## heading` in order and the self-check
    states the matching section count (catches the style 9-vs-8 self-contradiction
    the per-file token checks miss)."""
    mod = _load_validator()
    errs = mod.structural_checks(mod._load_all_texts())
    assert not errs, "structural section violations:\n" + "\n".join(errs)
