"""
Deploy-time frontend↔backend integration test (the "全面接口测试" gate).

After the deploy's ``/health`` + contract smoke prove the backend process is up
and a few GET routes resolve, this goes one layer deeper: it pulls the generated
FRONTEND's actual API-calling source + the shared OpenAPI contract, has an AI
step distill a TARGETED test plan (which endpoints the frontend really calls and
which response fields it parses), then EXECUTES that plan against the live
backend container over the shared docker network.

The gate is DETERMINISTIC (the AI only *plans*; pass/fail is decided by observed
responses), and TIERED — only a definitive failure on a ``critical`` case rolls
the deploy back:

  * a 5xx (the backend handler crashes — "后端报错太多"),
  * a JSON-expected response the frontend would fail to parse, or a contract/AI-
    declared required field missing from a 2xx body ("前端读取后端接口解析出现页面错误"),
  * the auth chain (login) returning 5xx.

Suspected / inconclusive signals — 4xx, cold-start timeouts, connection errors,
empty lists — are recorded as warnings and NEVER roll back a health+smoke-green
deploy. When no text provider is configured (or credits are exhausted), the plan
degrades to a deterministic contract-only set (GET no-param endpoints must not
5xx and must stay JSON-parseable) so the gate still runs, just without the AI
field-shape analysis. Comments in English (Code/core convention).
"""
import json
import logging
import os
import re
import zipfile
from typing import Callable, Optional

logger = logging.getLogger(__name__)

ITEST_TIMEOUT = int(os.getenv("APP_ITEST_TIMEOUT", "15"))      # per-request, seconds
ITEST_MAX_TESTS = int(os.getenv("APP_ITEST_MAX_TESTS", "14"))  # cap the executed plan
_FE_DIGEST_MAX = int(os.getenv("APP_ITEST_FE_DIGEST_CHARS", "14000"))

# Methods the gate is allowed to actually send. GET is idempotent; POST is a safe
# "create" against a throwaway, freshly-provisioned db. PUT/PATCH/DELETE are
# skipped — they mutate existing records (risking the seeded first screen) and
# usually need a path-param id we cannot fabricate safely.
_SAFE_METHODS = {"GET", "POST"}

# Common pagination-envelope keys the generated frontend parses (mirrors the
# first-screen probe's classifier so the two judge list payloads the same way).
_COLLECTION_KEYS = ("data", "items", "results", "records", "list", "rows", "content")

_FE_EXT = (".ts", ".tsx", ".js", ".jsx", ".vue", ".svelte")
# Directory stems where API-calling code usually lives, scored higher in the digest.
_FE_API_DIR_STEMS = (
    "api", "services", "service", "lib", "hooks", "store", "stores",
    "net", "request", "requests", "client", "http", "query",
)
# Substrings that mark a file as API-relevant (it talks to the backend).
_FE_API_HINTS = (
    "__API_BASE__", "VITE_API_BASE_URL", "fetch(", "axios", "useQuery",
    "useSWR", "useMutation", "/api", "apiBase", "API_BASE",
)


# --- tolerant JSON parse (mirrors contract_service._extract_json) -------------
def _extract_json(text: str) -> Optional[dict]:
    """Tolerant single-object JSON parse (code-fence / prefix tolerant)."""
    if not text:
        return None
    cleaned = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL | re.IGNORECASE)
    if fenced:
        cleaned = fenced.group(1).strip()
    if not cleaned.startswith("{"):
        match = re.search(r"(\{.*\})", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(1)
    try:
        value = json.loads(cleaned)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        logger.warning("integration test: failed to parse model JSON output")
        return None


# --- payload classification (pure) -------------------------------------------
def _records_for_field_check(body) -> list:
    """Return the candidate record dict(s) a required ``field`` should live on.

    Handles a bare object, a bare array (sampled), and the common pagination
    envelopes (optionally nested). Returns ``[]`` when no dict-record is reachable
    (empty collection / scalar / unreadable) — callers then SKIP field checks
    rather than fail, so an empty list never trips a false 'missing field'.
    """
    if isinstance(body, dict):
        for key in _COLLECTION_KEYS:
            v = body.get(key)
            if isinstance(v, list):
                dicts = [x for x in v if isinstance(x, dict)]
                return dicts[:5]  # empty collection -> [] -> skip (not fail)
            if isinstance(v, dict):
                inner = _records_for_field_check(v)
                if inner:
                    return inner
        return [body]
    if isinstance(body, list):
        return [x for x in body if isinstance(x, dict)][:5]
    return []


def _field_missing(body, field: str) -> bool:
    """True iff a reachable record exists AND none of the sampled records carry
    ``field``. Conservative (requires ALL sampled records to lack it) so a
    heterogeneous response never triggers a false rollback."""
    records = _records_for_field_check(body)
    if not records:
        return False
    return all(field not in r for r in records)


def _is_empty_list(body) -> bool:
    """True when a GET body is a recognizably EMPTY collection (advisory only)."""
    if isinstance(body, list):
        return len(body) == 0
    if isinstance(body, dict):
        for key in ("total", "count", "totalCount", "total_count"):
            v = body.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                return False
        for key in _COLLECTION_KEYS:
            v = body.get(key)
            if isinstance(v, list):
                return len(v) == 0
    return False


def _is_critical(case: dict) -> bool:
    """A case rolls the deploy back on a deterministic failure only when critical.
    Defaults to critical (strict): a 5xx / parse failure is always a real defect."""
    return str((case or {}).get("severity") or "critical").lower() == "critical"


def _looks_json(content_type: str) -> bool:
    """True when the server's own Content-Type declares JSON. The JSON-parse gate
    keys off this (the server's evidence), not the plan's *expectation* — so a
    text /health, a file/CSV/SSE download, or a cold-start HTML error page can't be
    mistaken for a broken JSON endpoint and roll a green deploy back."""
    return "json" in (content_type or "").lower()


# --- the deterministic judge (pure — no IO) ----------------------------------
def _judge_case(case: dict, *, status: Optional[int], parsed_ok: bool, body,
                has_body: bool = True, content_type: str = "") -> dict:
    """Judge one executed case from its observed response. Pure / deterministic.

    ``status is None`` means the request never completed (connection error /
    timeout) → inconclusive. Returns ``{outcome, deterministic_fail, reason}``
    where ``outcome ∈ {pass, fail, warn, inconclusive}`` and ``deterministic_fail``
    is True only on an OBSERVED definitive failure (a critical such case gates a
    rollback). ``has_body`` guards against flagging an intentionally empty body
    (204 / empty 200) as a parse failure; ``content_type`` keys the JSON-parse gate
    off the server's OWN declaration (not the plan's expectation).
    """
    expect = case.get("expect") or {}
    if status is None:
        return {"outcome": "inconclusive", "deterministic_fail": False,
                "reason": "请求未完成(连接错误/超时)"}
    if status >= 500:
        return {"outcome": "fail", "deterministic_fail": True, "reason": f"HTTP {status}(后端 5xx)"}
    # JSON parse failure is a DETERMINISTIC defect only on a 2xx whose server-
    # declared Content-Type IS JSON but the body won't parse (an unambiguous break
    # the frontend would choke on). When JSON was merely *expected* but the server
    # returns a non-JSON content type (text /health, a file/CSV/SSE download, a
    # cold-start error page), record a WARNING — never roll a green deploy back on
    # a content-type assumption. 3xx is excluded (`status < 300`), aligning with the
    # required-field branch's own 2xx success threshold below.
    if expect.get("json") and status < 300 and has_body and not parsed_ok:
        if _looks_json(content_type):
            return {"outcome": "fail", "deterministic_fail": True,
                    "reason": f"HTTP {status} 响应声明 JSON 但无法解析,前端将解析失败"}
        return {"outcome": "warn", "deterministic_fail": False,
                "reason": f"HTTP {status} 期望 JSON 但返回 {content_type or '非 JSON'}(记录不阻断)"}
    if status < 300 and parsed_ok:
        missing = [f for f in (expect.get("required_fields") or []) if isinstance(f, str)
                   and _field_missing(body, f)]
        if missing:
            return {"outcome": "fail", "deterministic_fail": True,
                    "reason": f"HTTP {status} 但缺少前端解析所需字段 {missing}"}
        if expect.get("nonempty_list") and _is_empty_list(body):
            return {"outcome": "warn", "deterministic_fail": False,
                    "reason": "列表为空(疑似 demo 数据未播种,记录不阻断)"}
        return {"outcome": "pass", "deterministic_fail": False, "reason": f"HTTP {status}"}
    if 400 <= status < 500:
        # Auth/validation/path nuance — record but never roll back (could be a
        # demo-credential mismatch or a missing path param, not a real defect).
        return {"outcome": "warn", "deterministic_fail": False, "reason": f"HTTP {status}(4xx,记录不阻断)"}
    return {"outcome": "pass", "deterministic_fail": False, "reason": f"HTTP {status}"}


# --- plan building -----------------------------------------------------------
def _sanitize_plan(tests: list) -> list:
    """Normalize an AI-proposed plan into safe, executable cases (capped)."""
    out: list[dict] = []
    for raw in tests:
        if not isinstance(raw, dict):
            continue
        path = raw.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            continue
        method = str(raw.get("method") or "GET").upper()
        expect = raw.get("expect") if isinstance(raw.get("expect"), dict) else {}
        req_fields = [f for f in (expect.get("required_fields") or []) if isinstance(f, str)][:12]
        severity = str(raw.get("severity") or "critical").lower()
        if severity not in ("critical", "warning"):
            severity = "critical"
        out.append({
            "id": str(raw.get("id") or f"{method} {path}")[:80],
            "method": method,
            "path": path,
            "auth": bool(raw.get("auth")),
            "query": raw.get("query") if isinstance(raw.get("query"), dict) else None,
            "body": raw.get("body") if isinstance(raw.get("body"), (dict, list)) else None,
            "expect": {
                "json": bool(expect.get("json", True)),
                "required_fields": req_fields,
                "nonempty_list": bool(expect.get("nonempty_list")),
            },
            "severity": severity,
            "frontend_reads": str(raw.get("frontend_reads") or "")[:200],
            "reason": str(raw.get("reason") or "")[:200],
        })
        if len(out) >= ITEST_MAX_TESTS:
            break
    return out


def _contract_has_login(paths: dict) -> bool:
    if not isinstance(paths, dict):
        return False
    for path, ops in paths.items():
        if not isinstance(path, str) or "{" in path or not isinstance(ops, dict):
            continue
        if "post" not in {str(k).lower() for k in ops}:
            continue
        low = path.lower()
        if any(h in low for h in ("login", "signin", "sign-in", "session", "token", "authenticate")):
            return True
    return False


def _contract_fallback_plan(contract: dict) -> list:
    """Deterministic plan from the contract alone (no AI, no FE-source insight).

    Each no-path-param GET endpoint must not 5xx and must stay JSON-parseable.
    Field-shape checks are intentionally OMITTED here — without the frontend's
    code we cannot know which fields it reads, so asserting contract `required`
    fields would risk false rollbacks. Those richer checks are the AI path's job.
    """
    openapi = (contract or {}).get("openapi") or {}
    paths = openapi.get("paths") if isinstance(openapi, dict) else {}
    has_login = _contract_has_login(paths if isinstance(paths, dict) else {})
    plan: list[dict] = []
    if isinstance(paths, dict):
        for path, ops in paths.items():
            if not isinstance(path, str) or "{" in path or not isinstance(ops, dict):
                continue
            methods = {str(k).lower() for k in ops}
            if "get" not in methods:
                continue
            plan.append({
                "id": f"GET {path}",
                "method": "GET", "path": path,
                # /health is public; other GETs may be owner-scoped — send auth if
                # the contract has a login (a token is acquired best-effort).
                "auth": has_login and path != "/health",
                "query": None, "body": None,
                "expect": {"json": True, "required_fields": [], "nonempty_list": False},
                "severity": "critical",
                "frontend_reads": "", "reason": "契约 GET 端点(确定性回退:仅校验非 5xx + 可解析)",
            })
            if len(plan) >= ITEST_MAX_TESTS:
                break
    return plan


# --- frontend source digest --------------------------------------------------
def _load_frontend_source(run) -> dict:
    """Extract the generated frontend source ({rel: bytes}) from its published zip."""
    if not run:
        return {}
    from backend.models.agent import AgentArtifact
    from backend.services.agent.files import artifact_abs_path

    art = (
        AgentArtifact.query.filter_by(run_id=run.id, domain_ref_type="code_frontend_project_zip")
        .order_by(AgentArtifact.created_at.desc())
        .first()
    )
    if not art or not art.storage_path:
        return {}
    abs_path = artifact_abs_path(art.storage_path)
    if not os.path.exists(abs_path):
        return {}
    files: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(abs_path, "r") as archive:
            for name in archive.namelist():
                if name.endswith("/"):
                    continue
                files[name] = archive.read(name)
    except Exception:  # noqa: BLE001 — unreadable zip → no FE insight, AI degrades
        logger.warning("integration test: could not read frontend source zip", exc_info=True)
        return {}
    return files


def _frontend_api_digest(files: dict, max_chars: int = _FE_DIGEST_MAX) -> str:
    """Pick the frontend's API-calling code (+ type defs) into a capped digest the
    AI uses to learn which endpoints are called and which fields are read."""
    if not files:
        return ""
    scored: list[tuple[int, str, str]] = []
    for rel, content in files.items():
        if not rel.lower().endswith(_FE_EXT):
            continue
        if isinstance(content, bytes):
            text = content.decode("utf-8", "ignore")
        else:
            text = str(content or "")
        if not text.strip():
            continue
        low = rel.lower()
        segments = set(low.split("/"))
        score = 0
        if segments & set(_FE_API_DIR_STEMS):
            score += 5
        if low.endswith("types.ts") or low.endswith("types.tsx") or "type" in segments:
            score += 3
        score += sum(1 for h in _FE_API_HINTS if h in text)
        if score <= 0:
            continue
        scored.append((score, rel, text))
    scored.sort(key=lambda x: (-x[0], x[1]))
    out: list[str] = []
    used = 0
    for _score, rel, text in scored:
        block = f"// FILE: {rel}\n{text[:4000]}"
        if used + len(block) + 2 > max_chars:
            remaining = max_chars - used
            if remaining > 80:
                out.append(block[:remaining])
            break
        out.append(block)
        used += len(block) + 2
    return "\n\n".join(out)


def _render_contract_block(contract: dict, max_chars: int = 9000) -> str:
    """Compact contract text for the AI plan prompt: endpoint list + OpenAPI JSON
    (paths + components.schemas, so the model can see required response fields)."""
    openapi = (contract or {}).get("openapi") or {}
    lines: list[str] = []
    paths = openapi.get("paths") if isinstance(openapi, dict) else None
    if isinstance(paths, dict) and paths:
        lines.append("### 端点清单(method path — 摘要)")
        for path, ops in list(paths.items())[:80]:
            if not isinstance(ops, dict):
                continue
            for method, op in ops.items():
                if str(method).lower() not in ("get", "post", "put", "patch", "delete"):
                    continue
                summary = (op.get("summary") or op.get("operationId") or "") if isinstance(op, dict) else ""
                lines.append(f"- {str(method).upper()} {path} — {summary}".rstrip(" —"))
    if isinstance(openapi, dict) and openapi:
        try:
            blob = json.dumps(openapi, ensure_ascii=False)
            lines.append("### OpenAPI 文档(JSON,权威 — schema/required/字段名以此为准)")
            lines.append(blob[:max_chars])
        except (TypeError, ValueError):
            pass
    if not lines:
        summary = (contract or {}).get("api_summary") or ""
        return summary[:max_chars]
    return "\n".join(lines)


def _ai_plan(
    project_id: str, contract: dict, fe_digest: str, user_id: str,
    team_id: Optional[str], run_id: Optional[str],
) -> Optional[list]:
    """AI-distilled targeted plan. Charged per deploy run; None → caller falls back
    to the deterministic contract-only plan (which runs free)."""
    from backend.services.ai import get_text_provider
    from backend.services.prompts import prompt_store

    try:
        provider = get_text_provider(force_new=True)  # background thread → fresh instance
    except Exception:  # noqa: BLE001
        return None
    if not provider or not provider.is_configured():
        return None
    try:
        template = prompt_store.get("code/integration_test_plan_prompt.txt")
    except Exception:  # noqa: BLE001 — prompt store down → deterministic fallback
        return None

    # Charge only right before the (paid) model call; insufficient credits → None
    # (deterministic plan still runs, free).
    from backend.services import pricing
    from backend.services.credit_service import charge

    if not charge(
        user_id=user_id, amount=pricing.CODE_FULLSTACK_INTEGRATION_TEST,
        operation="code_fullstack_itest", resource_type="agent_run",
        resource_id=run_id or project_id, description="fullstack integration test plan",
        team_id=team_id,
    ):
        return None

    prompt = (
        template
        .replace("[[CONTRACT]]", _render_contract_block(contract))
        .replace("[[FRONTEND_API_CODE]]", fe_digest or "(无可分析的前端源码,请仅据契约推断核心端点)")
    )
    try:
        result = provider.generate_text(prompt)
    except Exception as error:  # noqa: BLE001
        logger.warning("integration test plan model call raised: %s", error)
        return None
    if not result.success:
        logger.warning("integration test plan model returned failure: %s", result.error)
        return None
    parsed = _extract_json(result.text)
    if not parsed or not isinstance(parsed.get("tests"), list):
        return None
    return _sanitize_plan(parsed["tests"])


# --- token acquisition (reuses the deploy-service login matrix) ---------------
def _acquire_demo_token(base: str, project_id: str, cancelled: Callable[[], bool]) -> dict:
    """Best-effort demo login for auth-required cases. Reuses deploy_service's
    contract-driven login/OTP helpers + the seeded demo credentials.

    Returns ``{token, saw_5xx, saw_non5xx, attempted}``. The auth chain is only
    declared broken (→ rollback) when login was attempted, ≥1 attempt returned a
    5xx, AND NO attempt returned <500 — i.e. ``saw_5xx and not saw_non5xx``. A
    benign 4xx on the real login (demo credential / shape mismatch) sets
    ``saw_non5xx`` and so NEVER rolls back; this also neutralises ``_login_endpoints``
    matching token/session-ish non-login POSTs by substring.
    """
    import requests

    from backend.services.code import deploy_service as ds

    saw_5xx = False
    saw_non5xx = False
    attempted = False
    login_paths = ds._login_endpoints(project_id)

    def _try(bodies: list) -> Optional[str]:
        nonlocal saw_5xx, saw_non5xx, attempted
        for path in login_paths:
            if cancelled():
                return None
            for body in bodies:
                attempted = True
                try:
                    resp = requests.post(f"{base}{path}", json=body, timeout=ds.FIRST_SCREEN_TIMEOUT)
                except Exception:  # noqa: BLE001
                    continue
                if resp.status_code >= 500:
                    saw_5xx = True
                    continue
                # Any sub-500 response means a login handler is ALIVE (it merely
                # rejected the demo creds/shape) — record it so a benign 4xx
                # suppresses the auth-chain rollback.
                saw_non5xx = True
                if resp.status_code >= 400:
                    continue
                try:
                    tok = ds._find_token(resp.json())
                except ValueError:
                    tok = None
                if tok:
                    return tok
        return None

    token = _try([
        {"email": ds.SEED_DEMO_EMAIL, "password": ds.SEED_DEMO_PASSWORD},
        {"username": ds.SEED_DEMO_EMAIL, "password": ds.SEED_DEMO_PASSWORD},
        {"username": ds.SEED_DEMO_EMAIL.split("@")[0], "password": ds.SEED_DEMO_PASSWORD},
    ])
    if not token and not cancelled():
        # Passwordless / OTP fallback: fire a code request (ignored), then submit
        # the demo mandate's fixed dev code.
        for path in ds._otp_request_endpoints(project_id):
            for body in ({"phone": ds.SEED_DEMO_PHONE}, {"email": ds.SEED_DEMO_EMAIL}):
                try:
                    requests.post(f"{base}{path}", json=body, timeout=ds.FIRST_SCREEN_TIMEOUT)
                except Exception:  # noqa: BLE001
                    pass
        token = _try([
            {"phone": ds.SEED_DEMO_PHONE, "code": ds.SEED_DEMO_OTP},
            {"mobile": ds.SEED_DEMO_PHONE, "code": ds.SEED_DEMO_OTP},
            {"phone": ds.SEED_DEMO_PHONE, "otp": ds.SEED_DEMO_OTP},
            {"email": ds.SEED_DEMO_EMAIL, "code": ds.SEED_DEMO_OTP},
        ])
    return {"token": token, "saw_5xx": saw_5xx, "saw_non5xx": saw_non5xx, "attempted": attempted}


def _http_call(base: str, case: dict, headers: dict) -> tuple:
    """Send one case. Returns ``(status|None, parsed_ok, body, has_body, content_type)``."""
    import requests

    method = str(case.get("method") or "GET").upper()
    url = f"{base}{case.get('path') or '/'}"
    # Do NOT follow redirects — a 302 → HTML login page must stay a 3xx (judged a
    # non-defect), not resolve to a 200 text/html that masquerades as a JSON-parse
    # failure and rolls back a healthy deploy.
    kwargs: dict = {"timeout": ITEST_TIMEOUT, "headers": headers or {}, "allow_redirects": False}
    if isinstance(case.get("query"), dict):
        kwargs["params"] = case["query"]
    if method in ("POST", "PUT", "PATCH") and case.get("body") is not None:
        kwargs["json"] = case["body"]
    try:
        resp = requests.request(method, url, **kwargs)
    except Exception:  # noqa: BLE001 — connection error / timeout → inconclusive
        return None, False, None, False, ""
    has_body = bool((resp.content or b"").strip())
    content_type = resp.headers.get("Content-Type", "") or ""
    parsed_ok, body = False, None
    try:
        body = resp.json()
        parsed_ok = True
    except ValueError:
        parsed_ok = False
    return resp.status_code, parsed_ok, body, has_body, content_type


def _case_brief(case: dict) -> dict:
    return {"id": case.get("id"), "endpoint": f"{case.get('method')} {case.get('path')}",
            "severity": case.get("severity")}


# --- the entry point ---------------------------------------------------------
def run_integration_tests(
    *, project_id: str, user_id: str, team_id: Optional[str],
    container: str, port: int, frontend_run, run_id: Optional[str],
    cancelled: Callable[[], bool], plan: Optional[list] = None,
) -> dict:
    """Run the deploy-time frontend↔backend integration test gate.

    Returns ``{gate, reason, summary, cases, plan}`` where ``gate ∈ {pass, fail,
    inconclusive}``. Only ``gate == 'fail'`` should roll a deploy back — it is
    set ONLY when a ``critical`` case (or the auth chain) hit a deterministic
    failure (5xx / unparseable JSON / missing required field). The caller treats
    ``inconclusive`` like pass-with-note (never rolls back).

    Pass ``plan`` (the ``plan`` returned by a prior call) to RE-RUN the exact same
    cases against a now-repaired container — this skips the AI plan call entirely
    (no FE-source load, no re-charge), so the repair ladder re-tests cheaply.
    """
    from backend.services.code.fullstack import contract_service

    base = f"http://{container}:{port}"
    row = contract_service.get_ledger(project_id)
    contract = row.get_api_contract() if row else {}

    if plan is None:
        # Pull frontend resources so the AI can see what the app actually calls/parses.
        fe_files = _load_frontend_source(frontend_run)
        fe_digest = _frontend_api_digest(fe_files)
        plan = _ai_plan(project_id, contract, fe_digest, user_id, team_id, run_id)
        ai_used = plan is not None
        if not plan:  # no provider / no credits / unusable output → deterministic plan
            plan = _contract_fallback_plan(contract)
    else:
        ai_used = None  # reused plan: the first run already built + metered it
    if not plan:
        return {"gate": "inconclusive", "reason": "契约无可测端点(无结构化 OpenAPI 路径)",
                "summary": {"ai_used": ai_used, "degraded": not ai_used, "planned": 0,
                            "executed": 0, "failed": [], "warnings": []},
                "cases": [], "plan": []}

    need_auth = any(c.get("auth") for c in plan)
    token = None
    login_saw_5xx = False
    login_saw_non5xx = False
    login_attempted = False
    if need_auth and not cancelled():
        acq = _acquire_demo_token(base, project_id, cancelled)
        token = acq["token"]
        login_saw_5xx = acq["saw_5xx"]
        login_saw_non5xx = acq["saw_non5xx"]
        login_attempted = acq["attempted"]

    cases_out: list[dict] = []
    hard_failures: list[dict] = []
    warnings: list[dict] = []
    executed = 0
    for case in plan:
        if cancelled():
            break
        method = str(case.get("method") or "GET").upper()
        path = case.get("path") or "/"
        brief = _case_brief(case)
        # Guard rails (defend against an over-eager AI plan): only safe methods,
        # no path-param endpoints (we cannot fabricate ids safely).
        if method not in _SAFE_METHODS or "{" in path:
            cases_out.append({**brief, "outcome": "inconclusive", "deterministic_fail": False,
                              "reason": "跳过(写操作或含路径参数,避免破坏数据)"})
            continue
        auth = bool(case.get("auth"))
        if auth and not token:
            cases_out.append({**brief, "outcome": "inconclusive", "deterministic_fail": False,
                              "reason": "鉴权端点但未取得 demo token(凭据/验证码口径不符)"})
            continue
        headers = {"Authorization": f"Bearer {token}"} if (auth and token) else {}
        status, parsed_ok, body, has_body, content_type = _http_call(base, case, headers)
        executed += 1
        verdict = _judge_case(case, status=status, parsed_ok=parsed_ok, body=body,
                              has_body=has_body, content_type=content_type)
        res = {**brief, "status": status, "frontend_reads": case.get("frontend_reads") or "", **verdict}
        cases_out.append(res)
        if verdict["deterministic_fail"] and _is_critical(case):
            hard_failures.append(res)
        elif verdict["outcome"] in ("fail", "warn"):
            warnings.append(res)

    # Broken auth chain: login was attempted, ≥1 attempt returned 5xx, and NO
    # attempt returned <500 → the login handler crashes and the frontend could
    # never log in. A deterministic, critical failure. The `not login_saw_non5xx`
    # guard is essential: a benign 4xx on the real login (demo creds mismatch) must
    # NOT roll back, even if a token/session-ish non-login POST happened to 5xx.
    if (need_auth and token is None and login_saw_5xx and not login_saw_non5xx
            and any(_is_critical(c) and c.get("auth") for c in plan)):
        hard_failures.append({"id": "auth-chain", "endpoint": "POST <login>", "severity": "critical",
                              "status": 500, "outcome": "fail", "deterministic_fail": True,
                              "reason": "鉴权链路登录全部返回 5xx(无任何 <500 响应),前端将无法登录"})

    gate = "fail" if hard_failures else ("pass" if executed else "inconclusive")
    reason = hard_failures[0]["reason"] if hard_failures else (
        "" if gate == "pass" else "无可执行的接口或全部未下定论")

    def _slim(rows: list) -> list:
        return [{k: r.get(k) for k in ("id", "endpoint", "status", "reason")} for r in rows]

    summary = {
        "gate": gate, "ai_used": ai_used, "degraded": ai_used is False,
        "planned": len(plan), "executed": executed,
        "token": bool(token), "login_attempted": login_attempted,
        "failed": _slim(hard_failures), "warnings": _slim(warnings),
    }
    return {"gate": gate, "reason": reason, "summary": summary, "cases": cases_out, "plan": plan}


def format_failures_for_repair(itest: dict, *, contract_block: str = "", logs: str = "") -> str:
    """Render the deploy-time itest's deterministic failures into a digest the
    contract-repair agent uses to fix the backend. Lists each hard-failing case
    (endpoint / observed status / reason / what the frontend reads), then the
    container logs (5xx stack traces) and the contract (field/required authority).
    """
    cases = (itest or {}).get("cases") or []
    failing = [c for c in cases if c.get("deterministic_fail")]
    lines: list[str] = ["## 部署阶段前后端接口联调发现的确定性失败(逐条修复后端,使其符合契约且前端可解析)"]
    if not failing:
        lines.append("(无逐条用例明细)")
    for c in failing:
        sev = c.get("severity") or "critical"
        lines.append(f"- [{sev}] {c.get('endpoint')} → {c.get('reason')}")
        fr = c.get("frontend_reads")
        if fr:
            lines.append(f"    前端读取:{fr}")
    if logs:
        lines.append("\n## 运行中容器近期日志(stack trace,定位 5xx 根因)")
        lines.append(logs[-6000:])
    if contract_block:
        lines.append("\n## 共享 API 契约节选(字段/required 的权威依据)")
        lines.append(contract_block[-6000:])
    return "\n".join(lines)
