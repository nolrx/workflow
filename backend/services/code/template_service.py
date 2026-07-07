"""Template repository selection for Code project generation.

The frontend/backend agents can start from an existing scaffold instead of a
blank directory. This service clones (or reuses) a template repository, discovers
candidate scaffold directories, scores them against the project's documents, and
returns a binary-safe file map that can be seeded into the agent container.

The template repository may be private or temporarily unavailable. Selection is
therefore fail-soft: callers receive a selection object with ``files={}`` and a
warning, and the normal blank-generation path continues.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_TEMPLATE_REPO = "https://github.com/ai-worksflow/templates.git"
DEFAULT_CACHE_DIR = "/tmp/workflow-code-template-cache"

_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "dist",
    "build",
    ".next",
    "out",
    "target",
    "vendor",
    ".venv",
    "venv",
    "__pycache__",
    "coverage",
    ".pytest_cache",
    ".mypy_cache",
}
_SKIP_FILES = {".DS_Store", "Thumbs.db"}

_FRONTEND_MARKERS = {
    "vite.config.ts",
    "vite.config.js",
    "vite.config.mjs",
    "next.config.js",
    "next.config.mjs",
    "src/App.tsx",
    "src/App.jsx",
    "src/main.tsx",
    "src/main.jsx",
}
_BACKEND_MARKERS = {
    "Dockerfile",
    "requirements.txt",
    "pyproject.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "src/main/java",
}

_FRONTEND_HINTS = (
    "react",
    "vite",
    "typescript",
    "next",
    "vue",
    "svelte",
    "angular",
    "dashboard",
    "admin",
    "saas",
    "crm",
    "canvas",
    "figma",
)
_BACKEND_HINTS = (
    "python",
    "fastapi",
    "flask",
    "django",
    "node",
    "express",
    "nestjs",
    "nest",
    "go",
    "gin",
    "java",
    "spring",
    "postgres",
    "redis",
    "auth",
    "jwt",
    "ai",
)


@dataclass
class TemplateCandidate:
    lane: str
    path: Path
    rel_path: str
    name: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    stack: list[str] = field(default_factory=list)
    files_count: int = 0


@dataclass
class TemplateSelection:
    lane: str
    selected: bool
    files: dict[str, bytes] = field(default_factory=dict)
    repo_url: str = DEFAULT_TEMPLATE_REPO
    template_path: str = ""
    template_name: str = ""
    score: int = 0
    rationale: str = ""
    warning: str = ""

    def prompt_hint(self) -> str:
        if not self.selected:
            return ""
        bits = [
            f"模板名称: {self.template_name or self.template_path}",
            f"模板路径: {self.template_path}",
            f"匹配分: {self.score}",
            f"选择依据: {self.rationale or '按项目文档与模板元信息匹配'}",
            f"文件数: {len(self.files)}",
        ]
        return "\n".join(bits)

    def event_payload(self) -> dict[str, Any]:
        return {
            "selected": self.selected,
            "lane": self.lane,
            "template_repo": self.repo_url,
            "template_path": self.template_path,
            "template_name": self.template_name,
            "score": self.score,
            "files": len(self.files),
            "warning": self.warning,
        }


class CodeTemplateService:
    """Clone/cache and select scaffolds from a template repository."""

    def __init__(
        self,
        repo_url: Optional[str] = None,
        cache_dir: Optional[str] = None,
    ) -> None:
        self.repo_url = (repo_url or os.getenv("CODE_TEMPLATE_REPO") or DEFAULT_TEMPLATE_REPO).strip()
        self.cache_dir = Path(cache_dir or os.getenv("CODE_TEMPLATE_CACHE_DIR") or DEFAULT_CACHE_DIR)
        self.ref = (os.getenv("CODE_TEMPLATE_REF") or "").strip()
        self.enabled = _truthy(os.getenv("CODE_TEMPLATE_ENABLED", "1"))
        self.max_depth = int(os.getenv("CODE_TEMPLATE_SCAN_DEPTH", "4") or 4)
        self.max_file_bytes = int(os.getenv("CODE_TEMPLATE_MAX_FILE_BYTES", "1048576") or 1_048_576)
        self.max_total_bytes = int(os.getenv("CODE_TEMPLATE_MAX_TOTAL_BYTES", "20971520") or 20_971_520)
        self.max_files = int(os.getenv("CODE_TEMPLATE_MAX_FILES", "2500") or 2500)
        self.git_timeout = int(os.getenv("CODE_TEMPLATE_GIT_TIMEOUT", "180") or 180)
        self._lock = threading.Lock()

    def select(
        self,
        *,
        lane: str,
        requirement: str = "",
        requirements_doc: str = "",
        development_flow: str = "",
        documents_digest: str = "",
        style_prompt: str = "",
        contract_block: str = "",
    ) -> TemplateSelection:
        lane = "backend" if lane == "backend" else "frontend"
        if not self.enabled:
            return TemplateSelection(
                lane=lane,
                selected=False,
                repo_url=self.repo_url,
                warning="template selection disabled",
            )

        context = "\n".join(
            [
                requirement or "",
                requirements_doc or "",
                development_flow or "",
                documents_digest or "",
                style_prompt or "",
                contract_block or "",
            ]
        )
        try:
            repo = self._ensure_repo()
        except Exception as exc:  # noqa: BLE001 - fail-soft by design
            logger.warning("template repository unavailable: %s", exc)
            return TemplateSelection(
                lane=lane, selected=False, repo_url=self.repo_url, warning=str(exc)[:300]
            )

        candidates = self._discover(repo, lane)
        if candidates and self.ref:
            _annotate_branch_candidates(candidates, self.ref)
        # Some template repositories use one branch per scaffold rather than a
        # directory tree on main. If the checked-out ref has no candidates, score
        # remote branches by name (react-shadcn-template, python-fastapi-template,
        # etc.), clone the best branch, and discover candidates from that checkout.
        if not candidates and not self.ref:
            branch = self._select_branch(lane, context)
            if branch:
                try:
                    repo = self._ensure_repo(ref=branch)
                    candidates = self._discover(repo, lane)
                    _annotate_branch_candidates(candidates, branch)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("template branch %s unavailable: %s", branch, exc)
                    return TemplateSelection(
                        lane=lane,
                        selected=False,
                        repo_url=self.repo_url,
                        warning=f"template branch {branch} unavailable: {exc}",
                    )
        if not candidates:
            return TemplateSelection(
                lane=lane,
                selected=False,
                repo_url=self.repo_url,
                warning=f"no {lane} template candidates found in {repo}",
            )

        scored = [(self._score(candidate, context), candidate) for candidate in candidates]
        scored.sort(key=lambda item: (item[0], -len(item[1].rel_path)), reverse=True)
        score, candidate = scored[0]
        try:
            files = self._collect_files(candidate.path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to collect template %s: %s", candidate.rel_path, exc)
            return TemplateSelection(
                lane=lane,
                selected=False,
                repo_url=self.repo_url,
                warning=f"template {candidate.rel_path} could not be read: {exc}",
            )
        if not files:
            return TemplateSelection(
                lane=lane,
                selected=False,
                repo_url=self.repo_url,
                warning=f"template {candidate.rel_path} contains no usable files",
            )

        rationale = self._rationale(candidate, context)
        return TemplateSelection(
            lane=lane,
            selected=True,
            files=files,
            repo_url=self.repo_url,
            template_path=candidate.rel_path,
            template_name=candidate.name,
            score=score,
            rationale=rationale,
        )

    def _ensure_repo(self, ref: Optional[str] = None) -> Path:
        if not self.repo_url:
            raise RuntimeError("CODE_TEMPLATE_REPO is empty")
        local = Path(self.repo_url).expanduser()
        if local.exists() and local.is_dir():
            return local.resolve()

        selected_ref = (ref or self.ref or "").strip()
        target_name = _safe_repo_dir(self.repo_url)
        if selected_ref:
            target_name += "-" + _safe_ref(selected_ref)
        target = self.cache_dir / target_name
        with self._lock:
            if (target / ".git").exists():
                return target
            if target.exists():
                shutil.rmtree(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            cmd = ["git", "clone", "--depth", "1"]
            if selected_ref:
                cmd += ["--branch", selected_ref]
            cmd += [self.repo_url, str(target)]
            env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.git_timeout, env=env)
            if proc.returncode != 0:
                msg = (proc.stderr or proc.stdout or "git clone failed").strip()
                raise RuntimeError(msg)
            return target

    def _remote_branches(self) -> list[str]:
        local = Path(self.repo_url).expanduser()
        if local.exists() and local.is_dir():
            try:
                proc = subprocess.run(
                    ["git", "-C", str(local), "branch", "--format=%(refname:short)"],
                    capture_output=True,
                    text=True,
                    timeout=min(self.git_timeout, 60),
                )
            except subprocess.TimeoutExpired:
                return []
        else:
            try:
                proc = subprocess.run(
                    ["git", "ls-remote", "--heads", self.repo_url],
                    capture_output=True,
                    text=True,
                    timeout=min(self.git_timeout, 90),
                    env=dict(os.environ, GIT_TERMINAL_PROMPT="0"),
                )
            except subprocess.TimeoutExpired:
                logger.warning("template repository branch listing timed out: %s", self.repo_url)
                return []
        if proc.returncode != 0:
            return []
        branches: list[str] = []
        for line in proc.stdout.splitlines():
            text = line.strip()
            if not text:
                continue
            if "refs/heads/" in text:
                text = text.rsplit("refs/heads/", 1)[-1]
            branches.append(text.strip())
        return branches

    def _select_branch(self, lane: str, context: str) -> str:
        scored: list[tuple[int, str]] = []
        text = _norm(context)
        for branch in self._remote_branches():
            lane_guess = _lane_from_branch(branch)
            if lane_guess and lane_guess != lane:
                continue
            haystack = _norm(branch)
            if "template" not in haystack and not lane_guess:
                continue
            score = 40
            for hint in (_FRONTEND_HINTS if lane == "frontend" else _BACKEND_HINTS):
                if hint in haystack and hint in text:
                    score += 20
                elif hint in haystack:
                    score += 4
            if lane == "frontend" and not any(h in text for h in ("next", "vue", "svelte", "angular")):
                if "react" in haystack:
                    score += 8
                if "shadcn" in haystack:
                    score += 4
            if lane == "backend" and not any(h in text for h in ("django", "nestjs", "kratos", "gozero")):
                if "fastapi" in haystack:
                    score += 6
                if "nestjs" in haystack:
                    score += 3
            scored.append((score, branch))
        scored.sort(reverse=True)
        return scored[0][1] if scored else ""

    def _discover(self, root: Path, lane: str) -> list[TemplateCandidate]:
        candidates: list[TemplateCandidate] = []
        for path in _iter_dirs(root, self.max_depth):
            if any(part in _SKIP_DIRS for part in path.relative_to(root).parts):
                continue
            candidate = self._candidate_from_dir(root, path, lane)
            if candidate:
                candidates.append(candidate)
        return candidates

    def _candidate_from_dir(self, root: Path, path: Path, lane: str) -> Optional[TemplateCandidate]:
        files = {p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file()}
        dirs = {p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_dir()}
        if not files:
            return None

        meta = _read_template_meta(path)
        meta_lane = str(meta.get("lane") or meta.get("type") or meta.get("kind") or "").lower()
        if meta_lane in {"fe", "ui", "web"}:
            meta_lane = "frontend"
        if meta_lane in {"be", "api", "server"}:
            meta_lane = "backend"

        package = _read_package_json(path)
        package_deps = _package_words(package)
        all_paths = files | dirs

        frontend = bool(_FRONTEND_MARKERS & all_paths) or bool(
            package and package_deps & {"react", "vite", "next", "vue", "svelte", "@angular/core"}
        )
        backend = bool(_BACKEND_MARKERS & all_paths) or bool(
            package and package_deps & {"express", "fastify", "nestjs", "@nestjs/core", "koa"}
        )
        # A package with Vite/React is normally frontend even if it has a Dockerfile.
        if frontend and not (meta_lane == "backend"):
            inferred = "frontend"
        elif backend:
            inferred = "backend"
        else:
            inferred = meta_lane
        if meta_lane and meta_lane != lane and inferred != lane:
            return None
        if inferred != lane:
            return None

        rel_path = "." if path == root else path.relative_to(root).as_posix()
        name = str(meta.get("name") or package.get("name") or path.name)
        description = str(meta.get("description") or _read_readme_head(path)[:500])
        tags = _listish(meta.get("tags")) + _listish(meta.get("frameworks"))
        stack = _listish(meta.get("stack")) + sorted(package_deps)
        return TemplateCandidate(
            lane=lane,
            path=path,
            rel_path=rel_path,
            name=name,
            description=description,
            tags=tags,
            stack=stack,
            files_count=len(files),
        )

    def _score(self, candidate: TemplateCandidate, context: str) -> int:
        text = _norm(context)
        haystack = _norm(
            " ".join(
                [
                    candidate.rel_path,
                    candidate.name,
                    candidate.description,
                    " ".join(candidate.tags),
                    " ".join(candidate.stack),
                ]
            )
        )
        score = 50
        hints = _FRONTEND_HINTS if candidate.lane == "frontend" else _BACKEND_HINTS
        for hint in hints:
            if hint in haystack and hint in text:
                score += 12
        for token in set(re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", haystack)):
            if token in text:
                score += 1
        # Keep the current product default stable when docs don't specify a FE stack.
        if candidate.lane == "frontend" and not any(h in text for h in ("next", "vue", "svelte", "angular")):
            if "react" in haystack:
                score += 4
            if "vite" in haystack:
                score += 4
            if "typescript" in haystack or "ts" in haystack:
                score += 2
        return score

    def _rationale(self, candidate: TemplateCandidate, context: str) -> str:
        text = _norm(context)
        matched = []
        for hint in (_FRONTEND_HINTS if candidate.lane == "frontend" else _BACKEND_HINTS):
            if hint in text and hint in _norm(" ".join([candidate.rel_path, candidate.name, " ".join(candidate.stack)])):
                matched.append(hint)
        if matched:
            return "匹配项目文档关键词: " + ", ".join(matched[:8])
        if len(candidate.stack) > 0:
            return "未发现强关键词,按模板栈与默认生成路线选择"
        return "未发现强关键词,按可用模板顺序选择"

    def _collect_files(self, root: Path) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        total = 0
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if any(part in _SKIP_DIRS for part in rel.parts) or rel.name in _SKIP_FILES:
                continue
            if len(files) >= self.max_files:
                break
            size = path.stat().st_size
            if size > self.max_file_bytes or total + size > self.max_total_bytes:
                continue
            data = path.read_bytes()
            files[rel.as_posix()] = data
            total += len(data)
        return files


def _iter_dirs(root: Path, max_depth: int) -> list[Path]:
    dirs = [root]
    for path in root.rglob("*"):
        if not path.is_dir():
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if len(rel.parts) > max_depth:
            continue
        dirs.append(path)
    return dirs


def _read_template_meta(path: Path) -> dict:
    for name in ("template.json", ".template.json"):
        meta_path = path / name
        if meta_path.is_file():
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except (OSError, json.JSONDecodeError):
                return {}
    return {}


def _read_package_json(path: Path) -> dict:
    package_path = path / "package.json"
    if not package_path.is_file():
        return {}
    try:
        data = json.loads(package_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _read_readme_head(path: Path) -> str:
    for name in ("README.md", "readme.md"):
        readme = path / name
        if readme.is_file():
            try:
                return readme.read_text(encoding="utf-8", errors="ignore")[:4000]
            except OSError:
                return ""
    return ""


def _package_words(package: dict) -> set[str]:
    words = set()
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        deps = package.get(key)
        if isinstance(deps, dict):
            words.update(str(name).lower() for name in deps)
    for key in ("name", "description"):
        value = package.get(key)
        if value:
            words.update(re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{1,}", str(value).lower()))
    return words


def _listish(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).lower() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [v.strip().lower() for v in re.split(r"[,/\s]+", value) if v.strip()]
    return []


def _norm(value: str) -> str:
    return str(value or "").lower()


def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() not in {"0", "false", "no", "off"}


def _safe_repo_dir(repo_url: str) -> str:
    name = repo_url.rstrip("/").split("/")[-1] or "templates"
    name = name.removesuffix(".git")
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name).strip("-._") or "templates"
    return safe


def _safe_ref(ref: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", ref).strip("-._")
    return safe or "ref"


def _lane_from_branch(branch: str) -> str:
    name = _norm(branch)
    if any(token in name for token in ("react", "vue", "vanilla", "shoelace", "mui", "antd", "shadcn")):
        return "frontend"
    if any(token in name for token in ("python", "fastapi", "django", "node", "nestjs", "go-", "kratos", "gozero")):
        return "backend"
    return ""


def _annotate_branch_candidates(candidates: list[TemplateCandidate], branch: str) -> None:
    for candidate in candidates:
        candidate.rel_path = branch if candidate.rel_path == "." else f"{branch}/{candidate.rel_path}"


_service: CodeTemplateService | None = None


def get_code_template_service() -> CodeTemplateService:
    global _service
    if _service is None:
        _service = CodeTemplateService()
    return _service
