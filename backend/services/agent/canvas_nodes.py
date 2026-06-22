"""
Canvas node executors.

Each executable canvas node type (agent / merge / branch) has a small executor
that turns its wired-in upstream outputs into a single text output, reusing the
existing recorder step handle for live token streaming + prompt/response tracing.
Source nodes are not executed here — their content is pre-filled by the workflow.

Executors raise on failure; the workflow loop turns that into a failed step and
prunes the node's downstream subgraph.
"""
import logging
import re

from backend.services.agent.dag_engine import CanvasNodeView, NodeResult
from backend.services.ai.factory import build_text_provider
from backend.services.prompt_library import compose_recipe_prompt, compose_system_prompt

logger = logging.getLogger(__name__)

# A labeled-input pair: (upstream node label, its output text).
Inputs = list[tuple[str, str]]


def _join_inputs(inputs: Inputs, *, labeled: bool, separator: str, title_template: str) -> str:
    """Concatenate upstream outputs, optionally prefixing each with its label."""
    parts: list[str] = []
    for label, text in inputs:
        body = (text or "").strip()
        if not body:
            continue
        if labeled:
            try:
                heading = title_template.format(label=label)
            except (KeyError, IndexError, ValueError):
                heading = f"## {label}"
            parts.append(f"{heading}\n{body}")
        else:
            parts.append(body)
    return (separator or "\n\n").join(parts)


def _system_prefix(config: dict) -> str:
    """Build the role/recipe system prefix for an agent or branch node."""
    recipe_id = config.get("recipe_id")
    role_ids = config.get("role_ids") or []
    if recipe_id:
        try:
            return compose_recipe_prompt(recipe_id)
        except ValueError:
            logger.warning("Unknown recipe_id on canvas node: %s", recipe_id)
    if role_ids:
        try:
            return compose_system_prompt(role_ids[0], role_ids[1:])
        except ValueError:
            logger.warning("Unknown role_ids on canvas node: %s", role_ids)
    return ""


def _model_kwargs(config: dict) -> dict:
    """Extract per-node text model overrides (provider/model/base_url)."""
    model = config.get("model") or {}
    return {
        "provider": model.get("provider"),
        "model": model.get("model_name"),
        "base_url": model.get("base_url"),
    }


def _stream_text(provider, prompt: str, step) -> str:
    """Stream a generation through the step's live tracer, returning full text."""
    tracer = step.model_tracer()
    on_delta = step.model_delta_tracer()
    full = ""
    try:
        for piece in provider.generate_text_stream(prompt):
            if piece:
                on_delta(piece)
                full += piece
    except Exception as exc:  # noqa: BLE001 - convert to a node failure
        tracer(
            prompt=prompt, text="", success=False, error=str(exc),
            provider=provider.provider_name, model=provider.model,
        )
        raise RuntimeError(f"模型调用失败: {exc}") from exc
    text = full.strip()
    if not text:
        tracer(
            prompt=prompt, text="", success=False, error="empty",
            provider=provider.provider_name, model=provider.model,
        )
        raise RuntimeError("模型返回空内容")
    tracer(
        prompt=prompt, text=text, success=True,
        provider=provider.provider_name, model=provider.model,
    )
    return text


def run_agent_node(
    node: CanvasNodeView, inputs: Inputs, *, injected_ledger: str, step
) -> NodeResult:
    """Free-prompt LLM node: combine wired inputs + ledger + prompt, then generate."""
    config = node.config or {}
    user_prompt = (config.get("prompt") or "").strip()
    join_mode = config.get("input_join") or "labeled"
    combined = _join_inputs(
        inputs, labeled=(join_mode == "labeled"), separator="\n\n", title_template="## {label}"
    )

    provider = build_text_provider(**_model_kwargs(config))
    if provider is None:
        raise RuntimeError("未配置可用的文本模型（claude / gemini）")

    sections = []
    prefix = _system_prefix(config)
    if prefix:
        sections.append(prefix)
    if injected_ledger:
        sections.append(injected_ledger)
    if combined:
        sections.append(f"# 上游输入\n{combined}")
    sections.append(f"# 本节点任务\n{user_prompt or '基于上游输入产出结论。'}")
    prompt = "\n\n".join(sections)

    text = _stream_text(provider, prompt, step)
    return NodeResult(
        output_text=text,
        output_summary=f"已产出结论（{len(text)} 字符）。",
        reasoning_summary="按所选角色与模型，结合连入的上游文档与项目共识账本生成结论。",
        self_check=f"上游输入 {len(inputs)} 项；模型 {provider.provider_name}/{provider.model}。",
    )


def run_merge_node(node: CanvasNodeView, inputs: Inputs, *, step) -> NodeResult:
    """Concatenate all wired upstream outputs (no LLM call)."""
    config = node.config or {}
    merged = _join_inputs(
        inputs,
        labeled=bool(config.get("labeled", True)),
        separator=config.get("separator") or "\n\n---\n\n",
        title_template=config.get("title_template") or "## {label}",
    )
    return NodeResult(
        output_text=merged,
        output_summary=f"已合并 {len(inputs)} 个输入（{len(merged)} 字符）。",
        reasoning_summary="把连入的多份内容按顺序拼接，供下游节点统一消费。",
    )


def _classify_keyword(text: str, branches: list[dict], default_branch: str) -> tuple[str, str]:
    """Pick the first branch whose any keyword appears in the text."""
    lowered = (text or "").lower()
    for branch in branches:
        for kw in branch.get("keywords") or []:
            if kw and kw.lower() in lowered:
                return branch.get("key"), branch.get("label") or branch.get("key")
    return default_branch, _label_for(branches, default_branch)


def _label_for(branches: list[dict], key: str) -> str:
    for branch in branches:
        if branch.get("key") == key:
            return branch.get("label") or key
    return key


def run_branch_node(
    node: CanvasNodeView, inputs: Inputs, *, injected_ledger: str, step
) -> NodeResult:
    """Route downstream by classifying the combined upstream output into one branch."""
    config = node.config or {}
    branches = config.get("branches") or []
    if not branches:
        raise RuntimeError("条件分支节点未配置任何分支")
    keys = [b.get("key") for b in branches if b.get("key")]
    default_branch = config.get("default_branch") or keys[0]
    combined = _join_inputs(inputs, labeled=True, separator="\n\n", title_template="## {label}")

    mode = config.get("mode") or "llm_classify"
    if mode == "keyword":
        selected, selected_label = _classify_keyword(combined, branches, default_branch)
    else:
        provider = build_text_provider(**_model_kwargs(config))
        if provider is None:
            raise RuntimeError("未配置可用的文本模型（claude / gemini）")
        options = "\n".join(f"- {b.get('key')}: {b.get('label')}" for b in branches)
        instruction = config.get("prompt") or "判断上游结论属于以下哪一类，只回类别名。"
        prompt = (
            f"{instruction}\n\n# 可选类别（只回其中一个 key）\n{options}\n\n"
            f"# 上游内容\n{combined or '（无）'}\n\n只输出一个 key，不要其他内容。"
        )
        raw = _stream_text(provider, prompt, step)
        selected = _match_branch(raw, keys, default_branch)
        selected_label = _label_for(branches, selected)

    return NodeResult(
        output_text=combined,
        active_handles={selected},
        output_summary=f"选择分支：{selected_label}",
        reasoning_summary="根据上游结论判定走向，仅激活选中分支，其余下游子图跳过。",
        extra={"selected_branch": selected},
    )


def _match_branch(raw: str, keys: list[str], default_branch: str) -> str:
    """Pick the branch key the model named (exact token match, else substring)."""
    lowered = (raw or "").lower()
    tokens = set(re.findall(r"[a-z0-9_\-]+", lowered))
    for key in keys:
        if key and key.lower() in tokens:
            return key
    for key in keys:
        if key and key.lower() in lowered:
            return key
    return default_branch
