"""
Freeze a canvas's typed stage prompts to exact versions.

A draft canvas runs its stage nodes against the live HEAD prompt. "Freezing" (the
publish step) stamps each typed stage node with a ``prompt_pin`` — the exact
version of its contract's prompt at this moment — so the saved canvas keeps
running the same prompt body even after an admin later edits it. The pin lives in
the node's ``config`` (persisted in the canvas graph JSON), so freezing needs no
schema change.

Pure orchestration: ``head_pin(key) -> {key, version, hash}`` is injected (see
``PromptStore.head_pin``) so this stays unit-testable without Mongo.
"""
from collections.abc import Callable


def freeze_stage_prompts(
    nodes: list[dict],
    resolve_contract: Callable[[str], object],
    head_pin: Callable[[str], dict],
) -> tuple[list[dict], int]:
    """Stamp every typed stage node that has a prompt with a frozen pin.

    Returns ``(new_nodes, pinned_count)``. Freeform nodes, unknown contracts and
    stages without a prompt (preview / deploy) are returned unchanged.
    """
    new_nodes: list[dict] = []
    pinned = 0
    for node in nodes or []:
        data = node.get("data") or {}
        config = data.get("config") or {}
        key = config.get("contract_key")
        contract = resolve_contract(key) if key else None
        ref = getattr(contract, "prompt_ref", None) if contract else None
        if ref is None:
            new_nodes.append(node)
            continue
        new_config = {**config, "prompt_pin": head_pin(ref.key)}
        new_nodes.append({**node, "data": {**data, "config": new_config}})
        pinned += 1
    return new_nodes, pinned
