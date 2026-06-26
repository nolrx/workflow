"""
System prompt storage.

Mongo-backed store for all editable system prompts, with bundled defaults as the
seed source and read-only fallback. ``prompt_store`` is the singleton read/write
entry point; see ``store.py`` and ``defaults.py``.
"""
from backend.services.prompts.store import (
    COLLECTION,
    VERSIONS_COLLECTION,
    MongoUnavailableError,
    PromptStore,
    content_hash,
    prompt_store,
)

__all__ = [
    "COLLECTION",
    "VERSIONS_COLLECTION",
    "MongoUnavailableError",
    "PromptStore",
    "content_hash",
    "prompt_store",
]
