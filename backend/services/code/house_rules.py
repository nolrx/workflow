"""
Deterministic "house rules" linter for AI-generated projects.

The code-generation agent lane runs a less-reliable model (see the team runbook on
the gateway / DeepSeek lane), so prompt-level conventions are frequently ignored.
This module re-encodes the project's HARD, recurring, *mechanically detectable*
conventions as deterministic checks that run on the collected project files,
independent of model adherence. ``error`` violations feed back into the
verify->repair loop; ``warning`` violations are surfaced as advisories only.

Rules mirror the recurring failures documented in the runbook:
  * frontend must use HashRouter (subpath preview/deploy) — never BrowserRouter
  * frontend must not use Tailwind / UI kits / remote web fonts (style spec)
  * lockfiles must not pin a cnpm mirror (expired cert breaks ``npm ci``)
  * backend routes mount at the ROOT (the reverse proxy strips the /api prefix)
  * async SQLAlchemy must use the asyncpg driver, not psycopg2
  * backend must not hard-pin sslmode=require (the shared PG has no SSL)

Pure and side-effect-free, so it is unit-testable without Docker. ``check_frontend``
/ ``check_backend`` take the same ``{path: bytes|str}`` map that
``FrontendProjectService._collect`` produces and return a list of ``Violation``.
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from typing import Callable, Iterable

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"

# File extensions whose bytes are NOT source text — never scanned.
_BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".ico", ".bmp",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp4", ".webm", ".mov", ".mp3", ".wav", ".pdf", ".zip", ".gz", ".wasm",
}
_CODE_EXTS = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
_STYLE_EXTS = {".css", ".scss", ".sass", ".less"}
_BACKEND_CODE_EXTS = {".py", ".js", ".ts", ".mjs", ".cjs", ".go", ".java", ".kt", ".rb", ".php"}


@dataclass
class Violation:
    """A single house-rule breach in one file."""

    rule_id: str
    severity: str
    path: str
    message: str
    remediation: str

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "path": self.path,
            "message": self.message,
            "remediation": self.remediation,
        }


# --- file helpers -----------------------------------------------------------
def _decode(content) -> str:
    if isinstance(content, (bytes, bytearray)):
        return bytes(content).decode("utf-8", "ignore")
    return str(content or "")


def _text_files(files) -> list[tuple[str, str]]:
    """``{path: bytes|str}`` -> ``[(posix_path, text)]`` for text-like files only."""
    out: list[tuple[str, str]] = []
    for path, content in (files or {}).items():
        p = str(path).replace("\\", "/")
        if posixpath.splitext(p)[1].lower() in _BINARY_EXTS:
            continue
        text = _decode(content)
        if "\x00" in text[:2048]:  # binary sniff for extensionless blobs
            continue
        out.append((p, text))
    return out


def _ext(path: str) -> str:
    return posixpath.splitext(path)[1].lower()


def _basename(path: str) -> str:
    return posixpath.basename(path)


# --- frontend rules ---------------------------------------------------------
_BROWSER_ROUTER_RE = re.compile(r"\b(?:create)?BrowserRouter\b")
_TAILWIND_DEP_RE = re.compile(r'"tailwindcss"\s*:')
_TAILWIND_DIRECTIVE_RE = re.compile(r"@tailwind\b")
_REMOTE_FONT_RE = re.compile(
    r"fonts\.googleapis\.com|fonts\.gstatic\.com|@import\s+url\(\s*['\"]?https?://",
    re.IGNORECASE,
)
_MIRROR_RE = re.compile(
    r"registry\.npmmirror\.com|registry\.npm\.taobao\.org|npm\.taobao\.org|cnpmjs\.org"
)
_LOCKFILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json"}


def _fe_router(text_files: list[tuple[str, str]]) -> Iterable[Violation]:
    for path, text in text_files:
        if _ext(path) not in _CODE_EXTS:
            continue
        if _BROWSER_ROUTER_RE.search(text):
            yield Violation(
                "fe-no-browser-router", SEVERITY_ERROR, path,
                "使用了 BrowserRouter/createBrowserRouter,在子路径(/preview/<id>/、/app/<id>/)下会把路由跳回主域名导致白屏。",
                "改用 HashRouter / createHashRouter(react-router-dom),禁用 history 路由与根绝对跳转。",
            )


def _fe_tailwind(text_files: list[tuple[str, str]]) -> Iterable[Violation]:
    for path, text in text_files:
        base = _basename(path)
        if base == "package.json" and _TAILWIND_DEP_RE.search(text):
            yield Violation(
                "fe-no-tailwind", SEVERITY_ERROR, path,
                "package.json 依赖了 tailwindcss。",
                "本工程禁用 Tailwind 与第三方 UI 库,改用纯 CSS + 系统字体(见视觉风格规范)。",
            )
        elif base.startswith("tailwind.config."):
            yield Violation(
                "fe-no-tailwind", SEVERITY_ERROR, path,
                "存在 tailwind.config 配置文件。",
                "删除 Tailwind 配置文件,改用纯 CSS。",
            )
        elif _ext(path) in _STYLE_EXTS and _TAILWIND_DIRECTIVE_RE.search(text):
            yield Violation(
                "fe-no-tailwind", SEVERITY_ERROR, path,
                "样式文件使用了 @tailwind 指令。",
                "移除 @tailwind 指令,改用手写纯 CSS。",
            )


def _fe_remote_fonts(text_files: list[tuple[str, str]]) -> Iterable[Violation]:
    for path, text in text_files:
        if _ext(path) in (_STYLE_EXTS | _CODE_EXTS | {".html"}) and _REMOTE_FONT_RE.search(text):
            yield Violation(
                "fe-no-remote-fonts", SEVERITY_ERROR, path,
                "引用了远程 Web 字体(Google Fonts 或远程 @import)。",
                "改用系统字体栈(system-ui, -apple-system, 'Segoe UI', sans-serif),不要加载任何远程字体。",
            )


def _fe_lockfile_mirror(text_files: list[tuple[str, str]]) -> Iterable[Violation]:
    for path, text in text_files:
        if _basename(path) in _LOCKFILES and _MIRROR_RE.search(text):
            yield Violation(
                "fe-lockfile-mirror", SEVERITY_ERROR, path,
                "lockfile 的 resolved URL 指向 cnpm/taobao 镜像,镜像证书过期会让 Docker `npm ci` 报 CERT_HAS_EXPIRED。",
                "删除该 lockfile(让构建用官方源重新生成),或把 resolved URL 改回 https://registry.npmjs.org。",
            )


# --- backend rules ----------------------------------------------------------
_API_PREFIX_RES = [
    # Flask / FastAPI / Starlette decorators
    re.compile(r"@\w+\.(?:route|get|post|put|patch|delete|websocket)\(\s*['\"]/api(?:/|['\"])", re.I),
    # FastAPI APIRouter(prefix="/api") / include_router(..., prefix="/api")
    re.compile(r"prefix\s*=\s*['\"]/api(?:/|['\"])", re.I),
    # Express / Koa / Hono: app.use("/api"), router.get("/api/...")
    re.compile(r"\.(?:use|get|post|put|patch|delete|all)\(\s*['\"]/api(?:/|['\"])", re.I),
    # Spring: @RequestMapping("/api") / @GetMapping("/api/...")
    re.compile(r"@(?:Request|Get|Post|Put|Patch|Delete)Mapping\(\s*(?:value\s*=\s*)?['\"]/api", re.I),
    # Go (gin/echo/chi/mux): e.GET("/api/...") / r.HandleFunc("/api/...")
    re.compile(r"\.(?:GET|POST|PUT|PATCH|DELETE|Handle|HandleFunc|Group)\(\s*['\"]/api(?:/|['\"])"),
]


def _be_api_prefix(text_files: list[tuple[str, str]]) -> Iterable[Violation]:
    for path, text in text_files:
        if _ext(path) not in _BACKEND_CODE_EXTS:
            continue
        if any(rx.search(text) for rx in _API_PREFIX_RES):
            yield Violation(
                "be-no-api-prefix", SEVERITY_ERROR, path,
                "后端路由挂在 /api 前缀下;平台反代会剥掉 /app/<pid>/api 前缀,带 /api 会让每个接口 404。",
                "路由一律挂在根路径(如 /auth/login、/items、/health),不要加 /api 前缀。",
            )


def _be_async_driver(text_files: list[tuple[str, str]]) -> Iterable[Violation]:
    for path, text in text_files:
        if _ext(path) != ".py" or "create_async_engine" not in text:
            continue
        if "psycopg2" in text or re.search(r"postgresql://(?!.*\+asyncpg)", text):
            yield Violation(
                "be-async-driver-mismatch", SEVERITY_WARNING, path,
                "使用了 create_async_engine,但连接串/驱动是同步的(psycopg2 或裸 postgresql://)。",
                "异步引擎连接串须用 postgresql+asyncpg://;同步访问保留 psycopg2 即可。",
            )


def _be_sslmode(text_files: list[tuple[str, str]]) -> Iterable[Violation]:
    for path, text in text_files:
        if re.search(r"sslmode\s*=\s*require", text):
            yield Violation(
                "be-hardcoded-sslmode", SEVERITY_WARNING, path,
                "硬编码了 sslmode=require;平台共享 PostgreSQL 未开 SSL,会导致连接失败。",
                "不要硬编码 sslmode;交给平台注入(平台会按需钉 sslmode=disable)。",
            )


_FRONTEND_RULES: list[Callable[[list[tuple[str, str]]], Iterable[Violation]]] = [
    _fe_router, _fe_tailwind, _fe_remote_fonts, _fe_lockfile_mirror,
]
_BACKEND_RULES: list[Callable[[list[tuple[str, str]]], Iterable[Violation]]] = [
    _be_api_prefix, _be_async_driver, _be_sslmode,
]


# --- public API -------------------------------------------------------------
def check_frontend(files) -> list[Violation]:
    """Run the frontend house rules over a ``{path: bytes|str}`` file map."""
    tf = _text_files(files)
    out: list[Violation] = []
    for rule in _FRONTEND_RULES:
        out.extend(rule(tf))
    return out


def check_backend(files) -> list[Violation]:
    """Run the backend house rules over a ``{path: bytes|str}`` file map."""
    tf = _text_files(files)
    out: list[Violation] = []
    for rule in _BACKEND_RULES:
        out.extend(rule(tf))
    return out


def errors(violations: Iterable[Violation]) -> list[Violation]:
    return [v for v in violations if v.severity == SEVERITY_ERROR]


def warnings(violations: Iterable[Violation]) -> list[Violation]:
    return [v for v in violations if v.severity == SEVERITY_WARNING]


def has_blocking(violations: Iterable[Violation]) -> bool:
    """True when any ``error``-severity violation is present (drives repair)."""
    return any(v.severity == SEVERITY_ERROR for v in violations)


def summarize(violations: Iterable[Violation]) -> dict:
    vs = list(violations)
    return {
        "total": len(vs),
        "errors": len(errors(vs)),
        "warnings": len(warnings(vs)),
        "rule_ids": sorted({v.rule_id for v in vs}),
    }


def to_dicts(violations: Iterable[Violation]) -> list[dict]:
    return [v.to_dict() for v in violations]


def render_report(violations: Iterable[Violation]) -> str:
    """Compact markdown report (grouped error-first) for the repair-agent prompt."""
    vs = list(violations)
    if not vs:
        return ""
    lines = ["# 硬性规范检查(House Rules)发现以下问题,请在不破坏现有功能的前提下修复:"]

    def _group(items: list[Violation], title: str) -> None:
        if not items:
            return
        lines.append(f"\n## {title}")
        by_rule: dict[str, list[Violation]] = {}
        for v in items:
            by_rule.setdefault(v.rule_id, []).append(v)
        for rule_id, group in by_rule.items():
            head = group[0]
            paths = sorted({v.path for v in group})
            shown = ", ".join(paths[:8]) + (" 等" if len(paths) > 8 else "")
            lines.append(
                f"- [{rule_id}] {head.message}\n"
                f"  涉及文件:{shown}\n"
                f"  修复:{head.remediation}"
            )

    _group(errors(vs), "必须修复(否则视为不合格)")
    _group(warnings(vs), "建议修复(不强制)")
    return "\n".join(lines)


__all__ = [
    "Violation",
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "check_frontend",
    "check_backend",
    "errors",
    "warnings",
    "has_blocking",
    "summarize",
    "to_dicts",
    "render_report",
]
