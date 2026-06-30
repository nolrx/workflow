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
        "placeholders": ["CONTEXT_LEDGER", "REQUIREMENT", "REQUIREMENTS_DOC", "DEVELOPMENT_FLOW", "DOCUMENTS", "STYLE_PROMPT", "UI_BASELINE", "FIGMA_DESIGN", "CONTRACT"],
        "must_contain": ["gen-assets", "base: './'", "npm run build", "npm install", "src/assets", "src/components", "src/types.ts", "React", "Vite", "localStorage", "window.__API_BASE__",
            # Progressive-disclosure scaffold (P2-E): a navigable AGENTS.md + docs/
            # so iteration/repair runs read the repo instead of re-stuffing the prompt.
            "AGENTS.md",
            # Sub-path routing: the dist is served under /preview/<pid>/, so routing
            # MUST be hash-based (HashRouter) — history routing escapes to the main
            # domain. Keep the rule and its counter-example in the prompt.
            "HashRouter", "BrowserRouter",
            # Login/auth consistency: the frontend must consume the contract's
            # fixed auth shape — read `resp.data.token`, persist it, send it as
            # `Authorization: Bearer`, and gate routes on it (this is "前端怎么验证通过").
            "/auth/login", "Authorization: Bearer", "auth_token", "/auth/me",
            # API spec consistency: the client must uniformly unwrap the envelope
            # (take resp.data, treat success===false as error, read data.items).
            "resp.data", "data.items", "success===false",
            # Realtime client: when the contract declares a ws channel, the build
            # connects via the injected window.__WS_BASE__ and authenticates with
            # the same JWT as a ?token= query (browsers can't set a ws auth header).
            "__WS_BASE__", "WebSocket"],
    },
    "frontend_project_repair_prompt.txt": {
        "mode": "plain",
        "placeholders": [],
        "must_contain": ["npm run build", "base: './'", "HashRouter"],
    },
    "frontend_project_critic_prompt.txt": {
        "mode": "fill",
        # Skeptical rubric evaluator (P0-B): fed the deterministic house-rules +
        # runtime findings + the acceptance features list so its judgment is
        # grounded; its blocking_issues drive the verify->repair loop.
        "placeholders": ["REQUIREMENTS", "FEATURES", "STYLE_PROMPT", "HOUSE_RULES", "RUNTIME", "SOURCE"],
        "must_contain": [
            '"verdict"', '"scores"', '"feature_results"', '"fr_coverage"',
            '"blocking_issues"', '"advisory_issues"', '"issues"', '"summary"',
            "PASS", "CONCERNS", "FAIL",
            # rubric dimensions (Anthropic harness article) — turn subjective
            # "is it good?" into gradable terms.
            "design_quality", "originality", "craft", "functionality",
        ],
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
    # --- Full-stack pipeline (frontend + backend + middleware) ----------------
    "contract_synthesis_prompt.txt": {
        "mode": "fill",
        "placeholders": ["REQUIREMENTS", "FLOW", "DOCUMENTS"],
        "must_contain": [
            '"openapi"', '"api_summary"', '"tech_stack"', '"middleware"',
            '"datastores"', "/health", "PORT", "DATABASE_URL",
            # Login/auth single-source-of-truth: the contract is the only place
            # the token field name + carrier header are pinned. Guard the fixed
            # shape so a future trim can't drop it back to heuristic auth (the
            # cause of front/back login drift the harness has to guess around).
            "/auth/login", "securitySchemes", "bearerAuth", '"token"', "Authorization",
            # API spec single-source-of-truth: the uniform success/error envelope,
            # error-code vocabulary, pagination shape and field conventions are
            # pinned here so front/back can't drift on response shape (the cause of
            # "前端解析后端响应报错/白屏"). Guard so a trim can't drop the envelope.
            "ApiError", '"success"', '"items"', "VALIDATION_ERROR", "page_size", "snake_case",
            # Realtime single-source-of-truth: the `realtime` field + the fixed ws
            # convention (channel on the same /api base, routed by Upgrade; ?token=
            # query auth) are pinned here so a future trim can't drop the WebSocket
            # channel description (front/back would then have no agreed protocol).
            "realtime", "query_token", "WebSocket",
            # DB-schema single-source-of-truth: the machine-readable `db_schema` block
            # + the "non-PK string columns are TEXT (no narrow VARCHAR → 22001)" rule
            # are pinned here so a trim can't regress to length-less {type:string}
            # OpenAPI fields (the cause of "字段内容过长"/"架构缺字段" the deploy reconcile
            # then has to fix at runtime).
            '"db_schema"', "TEXT", "VARCHAR", "22001",
        ],
    },
    "backend_project_prompt.txt": {
        "mode": "fill",
        "placeholders": [
            "CONTEXT_LEDGER", "REQUIREMENT", "REQUIREMENTS_DOC",
            "DEVELOPMENT_FLOW", "DOCUMENTS", "CONTRACT", "MIDDLEWARE",
        ],
        "must_contain": [
            "Dockerfile", "/health", "PORT", "DATABASE_URL", "REDIS_URL", "EXPOSE 8080",
            # Architecture-discipline anchors: guard that the prompt keeps the
            # 6-question doctrine + delivered scaffolding (else a future trim /
            # a stale Mongo override could silently ship a doctrine-less prompt).
            "ARCHITECTURE.md", "AGENTS.md", "Makefile", "make test",
            # First-screen visibility: guard the empty-DB demo self-seed mandate
            # (the fix for "进入系统后什么都没有"). A future trim must not drop the
            # loginnable demo account, the env switch, or the 首屏 anchor.
            "SEED_DEMO_DATA", "demo@example.com", "自播种", "首屏",
            # Deployment-usable login (auth-method-agnostic): the demo account must
            # log in WITHOUT external deps — SMS/OTP/email-code logins need a fixed
            # dev 验证码, OAuth-only needs a local fallback. Guard these anchors so a
            # trim can't regress to an email/password-only mandate (the cause of the
            # SMS-login app whose /auth/login 400s after deploy).
            "验证码", "无外部依赖",
            # Login/auth consistency: the generated backend must implement the
            # contract's fixed auth shape verbatim (`data.token` JWT + a
            # `Authorization: Bearer` carrier) or the frontend can't log in.
            "/auth/login", "Authorization: Bearer", "bearerAuth",
            # API spec consistency: every handler must emit the uniform envelope
            # via shared helpers (success/error) or the frontend parse drifts.
            "success_response", "ApiError", "VALIDATION_ERROR", "snake_case",
            # Realtime: guard the conditional WebSocket implementation mandate
            # (channel on the same /api base + PORT, ?token= query auth) so a trim
            # can't drop it and leave a contract's realtime channels unimplemented.
            "realtime", "WebSocket",
            # Data layer: the backend ORM is now the SOLE schema author (init.sql
            # isn't pre-applied). Guard the non-PK-string-column→TEXT rule + the
            # contract db_schema as the authority so a trim can't regress to narrow
            # VARCHAR columns (22001 "字段内容过长") or to a missing-column ORM.
            "db_schema", "TEXT", "22001",
        ],
    },
    "backend_project_reinforce_prompt.txt": {
        "mode": "fill",
        "placeholders": [
            "CONTEXT_LEDGER", "REQUIREMENT", "REQUIREMENTS_DOC",
            "DEVELOPMENT_FLOW", "DOCUMENTS", "CONTRACT", "MIDDLEWARE",
        ],
        "must_contain": [
            "功能锚点", "二次功能补强", "状态机", "AI/提示词链路",
            "DATABASE_URL", "REDIS_URL", "/health", "ARCHITECTURE.md", "make test", "TODO",
            # First-screen visibility reinforcement (see backend_project_prompt).
            "SEED_DEMO_DATA", "自播种", "demo 账号",
            # Deployment-usable login: reinforce must re-check the demo login works
            # for the app's auth method (SMS/OTP fixed dev 验证码), not just seeding.
            "验证码",
            # Login/auth consistency: reinforce re-checks the fixed token field +
            # bearer header so a drifted auth shape gets repaired, not just seeded.
            "/auth/login", "Authorization: Bearer",
            # API spec consistency: reinforce re-checks the uniform envelope.
            "success_response", "ApiError",
            # Realtime: reinforce re-checks/completes the contract's ws channels.
            "WebSocket",
        ],
    },
    "backend_project_critic_prompt.txt": {
        "mode": "fill",
        # Anchor sources injected so the critic can do real FR/NFR/M traceability
        # (the contract alone may not preserve anchor numbering).
        "placeholders": ["CONTRACT", "REQUIREMENTS_DOC", "DEVELOPMENT_FLOW", "FEATURES", "HOUSE_RULES", "SOURCE"],
        "must_contain": [
            '"verdict"', '"endpoint_coverage"', '"fr_coverage"', '"issues"', '"summary"', "PASS", "CONCERNS", "FAIL",
            # Skeptical rubric evaluator (P0-B): rubric scores + per-feature
            # results + blocking_issues fed the deterministic house-rules report.
            '"scores"', '"feature_results"', '"blocking_issues"', '"advisory_issues"',
            "contract_conformance", "functional_completeness", "robustness", "security",
            # Acceptance must check first-screen visibility (demo self-seed).
            "首屏", "demo 账号",
            # ...and that the demo login is usable in the deployed env (SMS/OTP
            # logins need a fixed dev 验证码, else /auth/login 400s post-deploy).
            "验证码",
            # Login/auth consistency: acceptance must flag a drifted token field
            # name or bearer header (front/back login mismatch) as an issue.
            "/auth/login", "Authorization: Bearer",
            # API spec consistency: acceptance must flag responses that bypass the
            # uniform envelope / drift fields (front/back parse mismatch).
            "ApiError",
            # Realtime: acceptance must flag a contract ws channel that is missing
            # or whose handshake auth drifted off the ?token= query param.
            "WebSocket",
        ],
    },
    "backend_project_repair_prompt.txt": {
        "mode": "plain",
        "placeholders": [],
        "must_contain": ["Dockerfile", "/health", "PORT", "DATABASE_URL", "docker build"],
    },
    "backend_project_contract_repair_prompt.txt": {
        "mode": "plain",
        "placeholders": [],
        "must_contain": ["/health", "契约", "5xx", "前端", "最小改动", "docker build"],
    },
    "backend_project_5xx_repair_prompt.txt": {
        "mode": "plain",
        "placeholders": [],
        "must_contain": ["/health", "5xx", "数据库", "运行报错", "接口", "一次", "docker build"],
    },
    "integration_test_plan_prompt.txt": {
        "mode": "fill",
        "placeholders": ["CONTRACT", "FRONTEND_API_CODE"],
        "must_contain": [
            '"tests"', '"method"', '"path"', '"auth"', '"expect"',
            '"required_fields"', '"severity"', '"frontend_reads"',
            "critical", "warning", "/health", "GET", "POST",
        ],
    },
    "middleware_prompt.txt": {
        "mode": "fill",
        "placeholders": ["DATA_DESIGN", "MANIFEST", "CONTRACT"],
        "must_contain": [
            '"init_sql"', '"seed_sql"', '"entities"', '"summary"',
            # Division of labour: middleware seed_sql must NOT mint login accounts;
            # the loginnable demo account + first-screen data is the backend's
            # boot-time self-seed job (avoids the hash-mismatch double-write trap).
            "首屏", "自播种",
            # Schema consistency: init.sql (fallback) must follow the contract
            # db_schema and the non-PK-string→TEXT rule, so on the fallback path it
            # stays column-for-column consistent with the ORM (no narrow VARCHAR).
            "db_schema", "TEXT", "22001",
        ],
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


# --- cross-prompt semantic checks --------------------------------------------
# These go BEYOND per-file token presence: they assert that the SEMANTICS agreed
# across prompts stay mutually consistent. Per-file must_contain can pass 28/28
# while two prompts still contradict each other on the SAME protocol (the login
# token location, the frontend network-request rule). Each check below encodes
# one such cross-file invariant so a future single-file edit can't silently
# re-introduce a contradiction the mechanical checks don't see.

# Prompts that describe the login/auth protocol — all must pin token at data.token.
_AUTH_PROMPTS = [
    "contract_synthesis_prompt.txt",
    "backend_project_prompt.txt",
    "frontend_project_prompt.txt",
    "backend_project_critic_prompt.txt",
    "backend_project_reinforce_prompt.txt",
]
# Prescriptive "token lives at the top level" claims — the OLD convention, now
# wrong. NB: only PRESCRIPTIVE phrasings are listed; the corrected prompts contain
# PROSCRIPTIVE wording ("不得移出 data 放到响应体顶层") which must NOT match here.
_FORBIDDEN_TOPLEVEL_TOKEN = ["顶层固定 `token`", "响应体顶层固定 `token`", "置于响应体顶层"]

# Prompts that describe the realtime/WebSocket protocol — all must pin ws auth at
# the ?token= query param (a browser ws can't send an Authorization header), so a
# single-file edit can't drift ws auth to a header/cookie on one side only.
_WS_PROMPTS = [
    "contract_synthesis_prompt.txt",
    "backend_project_prompt.txt",
    "frontend_project_prompt.txt",
    "backend_project_critic_prompt.txt",
    "backend_project_reinforce_prompt.txt",
]

# Prompts that author the DB schema (contract emits db_schema; backend ORM + the
# middleware init.sql build it). All must pin the field-length invariant — non-PK
# string columns are unbounded TEXT, never a narrow VARCHAR — so a single-file edit
# can't reintroduce a VARCHAR(50) that 22001s ("字段内容过长") on the first long write.
_SCHEMA_LEN_PROMPTS = [
    "contract_synthesis_prompt.txt",
    "backend_project_prompt.txt",
    "middleware_prompt.txt",
]


def cross_prompt_checks(texts: dict[str, str]) -> list[str]:
    """Assert cross-file semantic invariants. ``texts`` maps filename -> content.
    Returns a list of human-readable violations (empty == all consistent)."""
    errs: list[str] = []

    # 1) Login token location is uniformly `data.token` (no front/back drift).
    for name in _AUTH_PROMPTS:
        text = texts.get(name, "")
        if "data.token" not in text:
            errs.append(f"{name}: auth prompt must pin the login token at `data.token` (not found)")
        for bad in _FORBIDDEN_TOPLEVEL_TOKEN:
            if bad in text:
                errs.append(f"{name}: contains stale top-level-token claim {bad!r} — token must be `data.token`")

    # 2) Frontend must NOT forbid its own same-origin backend API while requiring it.
    fe = texts.get("frontend_project_prompt.txt", "")
    if "同源后端 API" not in fe:
        errs.append("frontend_project_prompt.txt: must explicitly allow same-origin backend API "
                    "(the '禁止运行时网络请求' rule otherwise contradicts fullstack mode)")

    # 3) The backend critic must be FED the anchor sources it is asked to verify.
    critic = texts.get("backend_project_critic_prompt.txt", "")
    for ph in ("[[REQUIREMENTS_DOC]]", "[[DEVELOPMENT_FLOW]]"):
        if ph not in critic:
            errs.append(f"backend_project_critic_prompt.txt: must inject {ph} to verify FR/NFR/M traceability")

    # 4) WebSocket auth carrier is uniformly the ?token= query param (no front/back
    #    drift). A browser ws can't set an Authorization header, so every prompt
    #    describing the realtime channel must pin auth at ?token=.
    for name in _WS_PROMPTS:
        if "?token=" not in texts.get(name, ""):
            errs.append(f"{name}: realtime prompt must pin WebSocket auth at the `?token=` query param (not found)")

    # 5) Field-length invariant is uniform across the schema-authoring prompts:
    #    non-PK string columns are TEXT (no narrow VARCHAR), guarded by the 22001
    #    failure-mode anchor. A single-file trim can't drop it on one side only.
    for name in _SCHEMA_LEN_PROMPTS:
        text = texts.get(name, "")
        if "TEXT" not in text or "22001" not in text:
            errs.append(f"{name}: schema prompt must pin the non-PK-string→TEXT rule "
                        f"(TEXT + the 22001 over-length failure-mode anchor not both found)")

    return errs


# Prompts with a FIXED enumerated `## section` contract: the heading set + order
# + the self-check's stated section COUNT must agree. Catches the style 9-vs-8
# class of self-contradiction (output contract lists 9 sections, self-check says
# "八个") that per-file must_contain sails right past.
_NUMERALS = {6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}
STRUCTURED_SECTIONS: dict[str, list[str]] = {
    "requirements_prompt.txt": [
        "## 产品定位", "## 目标用户", "## 核心场景", "## 功能范围", "## 用户流程",
        "## 权限与账户", "## 数据对象", "## 非功能要求", "## 技术架构建议", "## 边界与待确认问题",
    ],
    "development_flow_prompt.txt": [
        "## 技术假设", "## 模块拆分", "## 数据设计", "## 接口设计", "## 前端页面/状态",
        "## 后端服务", "## AI/提示词链路", "## 开发里程碑", "## 验收标准", "## 风险清单",
    ],
    "style_prompt.txt": [
        "## 视觉定位", "## 基调", "## 布局规则", "## 组件风格", "## 色彩与字体",
        "## 交互反馈", "## 禁用事项", "## 缩略图生成提示词", "## 后续代码开发 UI 基调提示词",
    ],
}


def structural_checks(texts: dict[str, str]) -> list[str]:
    """Assert each fixed-section prompt lists every required `## heading` IN ORDER
    and that its self-check states the matching section COUNT word (so adding /
    dropping a section can't leave the self-check claiming the wrong number)."""
    errs: list[str] = []
    for name, headings in STRUCTURED_SECTIONS.items():
        text = texts.get(name, "")
        last = -1
        for h in headings:
            idx = text.find(h)
            if idx < 0:
                errs.append(f"{name}: missing required section heading {h!r}")
            elif idx < last:
                errs.append(f"{name}: section {h!r} is out of the contract order")
            else:
                last = idx
        numeral = _NUMERALS.get(len(headings))
        if numeral and f"{numeral}个" not in text:
            errs.append(f"{name}: self-check must state '{numeral}个' sections "
                        f"({len(headings)} `##` sections in the output contract)")
        for n, w in _NUMERALS.items():
            if n != len(headings) and f"{w}个内容小节" in text:
                errs.append(f"{name}: self-check claims '{w}个内容小节' but the "
                            f"contract enumerates {len(headings)} sections")
    return errs


def _load_all_texts() -> dict[str, str]:
    out: dict[str, str] = {}
    for name in MANIFEST:
        path = PROMPT_DIR / name
        if path.is_file():
            out[name] = path.read_text(encoding="utf-8")
    return out


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

    all_texts = _load_all_texts()
    semantic_ok = True
    for label, errs in (("cross-prompt semantics", cross_prompt_checks(all_texts)),
                        ("structural sections", structural_checks(all_texts))):
        if errs:
            semantic_ok = False
            failed += 1
            print(f"\n✗ {label}: FAIL")
            for e in errs:
                print(f"    - {e}")
        else:
            print(f"\n✓ {label}: ok")

    print(f"\n{total - failed}/{total} per-file passed"
          f"{' (+ semantic ok)' if semantic_ok else ' (semantic FAILED)'}.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
