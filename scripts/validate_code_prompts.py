#!/usr/bin/env python3
"""
Deterministic validator for the Code-domain prompt templates.

Run after rewriting ``backend/prompts/code/*.txt`` to mechanically prove the
prompts are still wiring-compatible with the pipeline:

  * ``.format`` prompts  → ``str.format(**dummy)`` must succeed (catches a stray
    single ``{``/``}`` that isn't a known placeholder), and every expected
    placeholder must be present.
  * ``[[KEY]]`` fill prompts → every expected ``[[KEY]]`` present, no unexpected
    ``[[...]]``, and NO ``{{``/``}}`` (braces are literal in fill mode, so an
    erroneously-escaped JSON example would emit ``{{`` into the live prompt).
  * ``plain`` prompts → no placeholders of either kind.
  * Per-file ``must_contain`` tokens → ledger-compat section headers, JSON field
    names / enum values, and operational contracts that downstream code depends on.

Exit code is non-zero if any file FAILs.

    uv run python scripts/validate_code_prompts.py
"""
from __future__ import annotations

import re
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent.parent / "backend" / "prompts" / "code"

DOC_TYPES = ["product_spec", "frontend_spec", "backend_spec", "data_model", "prompt_spec", "acceptance_plan"]

# mode: "format" | "fill" | "plain"
MANIFEST: dict[str, dict] = {
    "requirements_prompt.txt": {
        "mode": "format",
        "placeholders": ["system_prefix", "context_ledger", "requirement"],
        "must_contain": ["用户原始需求：", "产品定位", "目标用户", "功能范围", "技术架构", "边界与待确认"],
    },
    "development_flow_prompt.txt": {
        "mode": "format",
        "placeholders": ["system_prefix", "context_ledger", "requirements_doc"],
        "must_contain": ["技术假设", "里程碑", "验收"],
    },
    "style_prompt.txt": {
        "mode": "format",
        "placeholders": ["system_prefix", "context_ledger", "requirement", "styles"],
        "must_contain": ["基调", "缩略图生成提示词"],
    },
    "document_split_prompt.txt": {
        "mode": "format",
        "placeholders": ["system_prefix", "context_ledger", "requirements_doc", "development_flow"],
        "must_contain": ["prompt_expert", "document_type", "title", "content", *DOC_TYPES],
    },
    "requirements_clarify_prompt.txt": {
        "mode": "format",
        "placeholders": ["system_prefix", "context_ledger", "requirement", "requirements_doc"],
        "must_contain": ["allow_custom", "rationale", "options", "single", "multi"],
    },
    "requirements_revision_prompt.txt": {
        "mode": "format",
        "placeholders": ["system_prefix", "context_ledger", "instruction", "current_doc"],
        "must_contain": ["产品定位", "功能范围", "技术架构"],
    },
    "development_flow_revision_prompt.txt": {
        "mode": "format",
        "placeholders": ["system_prefix", "context_ledger", "instruction", "current_doc"],
        "must_contain": ["技术假设"],
    },
    "style_revision_prompt.txt": {
        "mode": "format",
        "placeholders": ["system_prefix", "context_ledger", "instruction", "current_doc"],
        "must_contain": ["基调"],
    },
    "document_split_revision_prompt.txt": {
        "mode": "format",
        "placeholders": ["system_prefix", "context_ledger", "instruction", "requirements_doc", "development_flow", "current_documents"],
        "must_contain": ["document_type", "prompt_expert", *DOC_TYPES],
    },
    "requirements_section_revision_prompt.txt": {
        "mode": "format",
        "placeholders": ["system_prefix", "context_ledger", "current_doc", "selected_text", "instruction"],
        "must_contain": ["选中片段"],
    },
    "development_flow_section_revision_prompt.txt": {
        "mode": "format",
        "placeholders": ["system_prefix", "context_ledger", "current_doc", "selected_text", "instruction"],
        "must_contain": ["选中片段"],
    },
    "document_section_revision_prompt.txt": {
        "mode": "format",
        "placeholders": ["system_prefix", "context_ledger", "current_doc", "selected_text", "instruction"],
        "must_contain": ["选中片段"],
    },
    "style_section_revision_prompt.txt": {
        "mode": "format",
        "placeholders": ["system_prefix", "context_ledger", "current_doc", "selected_text", "instruction"],
        "must_contain": ["选中片段"],
    },
    "frontend_project_prompt.txt": {
        "mode": "fill",
        "placeholders": ["CONTEXT_LEDGER", "REQUIREMENT", "REQUIREMENTS_DOC", "DEVELOPMENT_FLOW", "DOCUMENTS", "STYLE_PROMPT", "UI_BASELINE", "FIGMA_DESIGN"],
        "must_contain": ["gen-assets", "base: './'", "npm run build", "npm install", "src/assets", "src/components", "src/types.ts", "React", "Vite", "localStorage"],
    },
    "frontend_project_repair_prompt.txt": {
        "mode": "plain",
        "placeholders": [],
        "must_contain": ["npm run build", "base: './'"],
    },
    "frontend_project_critic_prompt.txt": {
        "mode": "fill",
        "placeholders": ["REQUIREMENTS", "STYLE_PROMPT", "SOURCE"],
        "must_contain": ['"verdict"', '"fr_coverage"', '"issues"', '"summary"', "PASS", "CONCERNS", "FAIL"],
    },
    "consistency_gate_prompt.txt": {
        "mode": "fill",
        "placeholders": ["FINGERPRINT", "SUMMARY", "STEP_KEY"],
        "must_contain": ['"verdict"', '"conflicts"', "PASS", "CONCERNS", "FAIL", "requirement"],
    },
    "html_to_figma_ir_prompt.txt": {
        "mode": "fill",
        "placeholders": ["HTML"],
        "must_contain": ['"ir_version"', '"root"', "FRAME", "RECTANGLE", "TEXT"],
    },
    "figma_slice_prompt.txt": {
        "mode": "fill",
        "placeholders": ["IMAGE_WIDTH", "IMAGE_HEIGHT", "STYLE_PROMPT", "CONTEXT_LEDGER", "NAME"],
        # width/height/name are tiny scalars injected in several places — fine to repeat.
        "allow_repeat": ["IMAGE_WIDTH", "IMAGE_HEIGHT", "NAME"],
        "must_contain": ["/out/ir.json", "snake_case", "ir_version", "sliced"],
    },
}

PLACEHOLDER_RE = re.compile(r"(?<!\{)\{([a-zA-Z_][a-zA-Z0-9_]*)\}(?!\})")
FILL_RE = re.compile(r"\[\[([A-Z_]+)\]\]")


def validate_one(name: str, spec: dict, text: str) -> list[str]:
    errs: list[str] = []
    mode = spec["mode"]
    placeholders = spec["placeholders"]

    if mode == "format":
        # 1) every expected placeholder present
        for p in placeholders:
            if ("{" + p + "}") not in text:
                errs.append(f"missing placeholder {{{p}}}")
        # 2) str.format must succeed with exactly the known placeholders
        dummy = {p: "·" for p in placeholders}
        try:
            text.format(**dummy)
        except KeyError as e:
            errs.append(f"str.format KeyError {e} — a single-brace token is not a known placeholder "
                        f"(escape literal braces as {{{{ }}}}, or it's an unexpected placeholder)")
        except (IndexError, ValueError) as e:
            errs.append(f"str.format {type(e).__name__}: {e} — likely an unescaped single '{{' or '}}' "
                        f"(literal braces must be doubled)")
        # 3) system_prefix + context_ledger must lead .format prompts
        if "system_prefix" in placeholders:
            head = text.lstrip()
            if not head.startswith("{system_prefix}"):
                errs.append("{system_prefix} must be the first block")
        # 4) no stray fill-style tokens
        stray = set(FILL_RE.findall(text))
        if stray:
            errs.append(f"unexpected [[KEY]] tokens in a format prompt: {sorted(stray)}")

    elif mode == "fill":
        all_found = FILL_RE.findall(text)  # list — keeps repeats
        found = set(all_found)
        expected = set(placeholders)
        # Tokens that inject large content must appear once (str.replace is
        # replace-all → duplicating a doc bloats the prompt). Tiny SCALAR tokens
        # (e.g. width/height/name) may legitimately repeat — list them in
        # "allow_repeat".
        allow_repeat = set(spec.get("allow_repeat", []))
        for p in placeholders:
            n = all_found.count(p)
            if n == 0:
                errs.append(f"missing placeholder [[{p}]]")
            elif n > 1 and p not in allow_repeat:
                errs.append(
                    f"placeholder [[{p}]] appears {n}x — fill uses str.replace (replace-all), "
                    f"so its injected content gets DUPLICATED (prompt bloat); keep each token exactly once"
                )
        unexpected = found - expected
        if unexpected:
            errs.append(f"unexpected [[KEY]] tokens: {sorted(unexpected)}")
        # NB: no "{{"/"}}" check here — fill mode uses str.replace (braces are
        # literal and irrelevant), and nested JSON examples legitimately contain
        # adjacent "}}" (e.g. an inner object that closes with its parent).

    elif mode == "plain":
        if FILL_RE.findall(text):
            errs.append(f"plain prompt should have no [[KEY]] tokens: {sorted(set(FILL_RE.findall(text)))}")
        if PLACEHOLDER_RE.findall(text):
            errs.append(f"plain prompt should have no {{placeholder}} tokens: {sorted(set(PLACEHOLDER_RE.findall(text)))}")

    # must_contain tokens (ledger headers, JSON fields/enums, operational contracts)
    for token in spec.get("must_contain", []):
        if token not in text:
            errs.append(f"missing required token: {token!r}")

    return errs


def main() -> int:
    total = len(MANIFEST)
    failed = 0
    print(f"Validating {total} Code-domain prompts in {PROMPT_DIR}\n")
    for name in sorted(MANIFEST):
        path = PROMPT_DIR / name
        if not path.is_file():
            print(f"✗ {name}: FILE MISSING")
            failed += 1
            continue
        text = path.read_text(encoding="utf-8")
        errs = validate_one(name, MANIFEST[name], text)
        if errs:
            failed += 1
            print(f"✗ {name}: FAIL")
            for e in errs:
                print(f"    - {e}")
        else:
            print(f"✓ {name}: ok  ({MANIFEST[name]['mode']}, {len(text)} chars)")

    print(f"\n{total - failed}/{total} passed.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
