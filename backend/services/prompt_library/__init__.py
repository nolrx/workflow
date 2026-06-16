"""
Reusable system prompt prefix library.
"""
from backend.services.prompt_library.internet_roles import (
    BASE_SYSTEM_PREFIX,
    OUTPUT_CONTRACT,
    PROMPT_RECIPE_EXAMPLES,
    PROMPT_PREFIXES,
    PROMPT_RECIPES,
    ROUTER_PREFIX,
    SYSTEM_PROMPT_ASSEMBLY_GUIDE,
    PromptPrefix,
    PromptRoute,
    compose_recipe_prompt,
    compose_system_prompt,
    get_prefix,
    list_prefixes,
    route_prefixes,
)

__all__ = [
    "BASE_SYSTEM_PREFIX",
    "OUTPUT_CONTRACT",
    "PROMPT_RECIPE_EXAMPLES",
    "PROMPT_PREFIXES",
    "PROMPT_RECIPES",
    "ROUTER_PREFIX",
    "SYSTEM_PROMPT_ASSEMBLY_GUIDE",
    "PromptPrefix",
    "PromptRoute",
    "compose_recipe_prompt",
    "compose_system_prompt",
    "get_prefix",
    "list_prefixes",
    "route_prefixes",
]
