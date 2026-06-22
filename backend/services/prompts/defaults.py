"""
Bundled default prompts.

The single source of the *built-in* prompt text that ships with the app. These
defaults are used to:

  * seed MongoDB on startup (one document per key, only when missing), and
  * fall back to when MongoDB is unreachable (read-only).

Keys use a ``<scope>/<name>`` convention:

  * ``code/<filename.txt>``   — the Code-domain ``.txt`` templates under
    ``backend/prompts/code`` (consumed via ``str.format`` by the Code services).
  * ``prefix/<id>``           — one per role prefix in the prompt library.
  * ``special/<NAME>``        — the shared building blocks (base prefix, output
    contract, router prefix, assembly guide).

NOTE: importing this module reads the ``.txt`` files from disk once and imports
``internet_roles`` (which does not import this package at module load), so there
is no import cycle. ``internet_roles`` resolves overrides lazily at call time.
"""
from dataclasses import dataclass
from pathlib import Path

from backend.services.prompt_library import internet_roles as roles

CODE_PROMPT_DIR = Path(__file__).parent.parent.parent / "prompts" / "code"


@dataclass(frozen=True)
class PromptDefault:
    """One built-in prompt: its key, classification, and default content."""

    key: str
    scope: str  # code | prefix | special
    name: str
    description: str
    category: str
    content: str


# Shared building blocks (scope=special). (NAME, display name, description, text)
_SPECIAL: tuple[tuple[str, str, str, str], ...] = (
    (
        "BASE_SYSTEM_PREFIX",
        "基础系统前缀",
        "所有角色提示词共享的基础人设与工作原则。",
        roles.BASE_SYSTEM_PREFIX,
    ),
    (
        "OUTPUT_CONTRACT",
        "输出契约",
        "约束模型输出结构的通用契约。",
        roles.OUTPUT_CONTRACT,
    ),
    (
        "ROUTER_PREFIX",
        "任务路由前缀",
        "任务路由 Agent 用于判断应加载哪些场景前缀的提示词。",
        roles.ROUTER_PREFIX,
    ),
    (
        "SYSTEM_PROMPT_ASSEMBLY_GUIDE",
        "系统提示词组装指南",
        "说明如何把基础前缀、场景前缀与输出契约组装为完整系统提示词。",
        roles.SYSTEM_PROMPT_ASSEMBLY_GUIDE,
    ),
)


def _code_defaults() -> list[PromptDefault]:
    out: list[PromptDefault] = []
    if not CODE_PROMPT_DIR.is_dir():
        return out
    for path in sorted(CODE_PROMPT_DIR.glob("*.txt")):
        out.append(
            PromptDefault(
                key=f"code/{path.name}",
                scope="code",
                name=path.stem,
                description=f"Code 域提示词模板:{path.name}",
                category="code",
                content=path.read_text(encoding="utf-8"),
            )
        )
    return out


def _prefix_defaults() -> list[PromptDefault]:
    return [
        PromptDefault(
            key=f"prefix/{pid}",
            scope="prefix",
            name=prefix.name,
            description=prefix.description,
            category=prefix.category,
            content=prefix.text,
        )
        for pid, prefix in roles.PROMPT_PREFIXES.items()
    ]


def _special_defaults() -> list[PromptDefault]:
    return [
        PromptDefault(
            key=f"special/{name}",
            scope="special",
            name=display,
            description=description,
            category="special",
            content=content,
        )
        for name, display, description, content in _SPECIAL
    ]


_index: dict[str, PromptDefault] | None = None
_ordered: list[PromptDefault] | None = None


def _build() -> list[PromptDefault]:
    return [*_code_defaults(), *_prefix_defaults(), *_special_defaults()]


def iter_default_prompts() -> list[PromptDefault]:
    """Return every built-in prompt (code templates, prefixes, special blocks)."""
    global _ordered
    if _ordered is None:
        _ordered = _build()
    return _ordered


def get_default(key: str) -> PromptDefault | None:
    """Return the built-in default for ``key``, or ``None`` if there isn't one."""
    global _index
    if _index is None:
        _index = {d.key: d for d in iter_default_prompts()}
    return _index.get(key)


def get_default_content(key: str) -> str | None:
    """Return just the default content string for ``key`` (or ``None``)."""
    default = get_default(key)
    return default.content if default else None
