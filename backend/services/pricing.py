"""
Central credit pricing table.

Single source of truth for how many credits each AI-consuming operation costs.
Every value is overridable via an environment variable so ops can tune pricing
without a redeploy. Keep this module dependency-free (no Flask / DB imports) so
it can be imported from routes, services and background tasks alike.

NOTE: this lives under ``backend/services`` (not ``backend/config``) on purpose —
``backend/config.py`` is an existing module loaded as ``backend.config.<Name>Config``
in ``app.py``; turning ``config`` into a package would shadow it and break startup.
"""
import os


def _credits(env_var: str, default: int) -> int:
    """Read a non-negative credit cost from the environment, falling back to default."""
    try:
        value = int(os.getenv(env_var, default))
    except (TypeError, ValueError):
        return default
    return max(0, value)


# --- PPT domain ---------------------------------------------------------------
PPT_OUTLINE = _credits("PRICE_PPT_OUTLINE", 1)              # per generate/outline call
PPT_DESCRIPTION_PAGE = _credits("PRICE_PPT_DESC_PAGE", 1)   # per page description
PPT_IMAGE_PAGE = _credits("PRICE_PPT_IMAGE_PAGE", 2)        # per page image

# --- RedBook domain -----------------------------------------------------------
REDBOOK_OUTLINE = _credits("PRICE_RB_OUTLINE", 1)
REDBOOK_CONTENT = _credits("PRICE_RB_CONTENT", 1)
REDBOOK_IMAGE_PAGE = _credits("PRICE_RB_IMAGE_PAGE", 2)     # per generated image

# --- Code (agent) domain ------------------------------------------------------
CODE_FULL_GENERATION = _credits("PRICE_CODE_FULL", 6)       # base workflow cost

# Up-front reservation for the full code workflow (requirements -> flow ->
# document split -> style -> previews -> publish).
CODE_FULL_GENERATION_TOTAL = CODE_FULL_GENERATION

# Frontend project generation + adversarial review (+ one repair pass) — heavier.
CODE_FRONTEND_GENERATION = _credits("PRICE_CODE_FRONTEND", 10)


# Operation label -> (resource_type, unit_cost). Used when writing CreditTransaction
# records so the audit log carries a stable operation/resource vocabulary.
OPERATION = {
    "ppt_outline": ("ppt_project", PPT_OUTLINE),
    "ppt_description": ("ppt_project", PPT_DESCRIPTION_PAGE),
    "ppt_image": ("ppt_project", PPT_IMAGE_PAGE),
    "redbook_outline": ("redbook_task", REDBOOK_OUTLINE),
    "redbook_content": ("redbook_task", REDBOOK_CONTENT),
    "redbook_image": ("redbook_task", REDBOOK_IMAGE_PAGE),
    "agent_run": ("agent_run", CODE_FULL_GENERATION_TOTAL),
}
