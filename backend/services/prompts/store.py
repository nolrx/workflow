"""
Prompt store — Mongo-backed, default-fallback resolution of system prompts.

``prompt_store.get(key)`` is the single read path used across the backend:

  * If MongoDB holds a document for ``key`` → return its ``content`` (the admin
    may have edited it online).
  * Otherwise → return the bundled default from ``defaults.py``.

Reads are cached in-process for a short TTL so hot paths (the Code services call
``get`` a handful of times per run) don't hit Mongo every time, while online
edits still become visible quickly across gunicorn workers. Writes invalidate
the affected key immediately in the editing worker.

When Mongo is unreachable everything still works read-only off the bundled
defaults; the admin write paths raise ``MongoUnavailableError`` so the API can
report a clear 503 instead of silently dropping an edit.
"""
import logging
import threading
import time
from datetime import datetime

from pymongo.errors import PyMongoError

from backend.services.mongo import get_mongo_db
from backend.services.prompts import defaults

logger = logging.getLogger(__name__)

COLLECTION = "prompts"
_CACHE_TTL_SECONDS = 60.0


class MongoUnavailableError(RuntimeError):
    """Raised by write operations when MongoDB is not reachable."""


class PromptStore:
    """Resolve, list and edit system prompts backed by MongoDB."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    # --- read path ----------------------------------------------------------
    def get(self, key: str) -> str:
        """Return the current content for ``key`` (override or default).

        Raises ``KeyError`` if the key is neither stored nor a known default —
        that indicates a programming error (a caller asked for a prompt that
        does not exist), matching the old "file not found" behaviour.
        """
        now = time.time()
        cached = self._cache.get(key)
        if cached and cached[1] > now:
            return cached[0]

        content = self._resolve(key)
        if content is None:
            raise KeyError(f"Unknown prompt key: {key}")
        with self._lock:
            self._cache[key] = (content, time.time() + _CACHE_TTL_SECONDS)
        return content

    def _resolve(self, key: str) -> str | None:
        db = get_mongo_db()
        if db is not None:
            try:
                doc = db[COLLECTION].find_one({"_id": key}, {"content": 1})
                if doc and doc.get("content") is not None:
                    return doc["content"]
            except PyMongoError as error:
                logger.warning("Mongo read failed for prompt %s: %s", key, error)
        return defaults.get_default_content(key)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._cache.pop(key, None)

    def reset_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    # --- seeding ------------------------------------------------------------
    def seed_defaults(self) -> int:
        """Insert any missing default prompts into Mongo (idempotent).

        Never overwrites an existing document, so admin edits are preserved and
        newly-added default keys get backfilled. Returns the number inserted.
        No-op (logs a warning) when Mongo is unavailable.
        """
        db = get_mongo_db()
        if db is None:
            logger.warning(
                "MongoDB unavailable; skipping prompt seeding (using bundled defaults)."
            )
            return 0
        coll = db[COLLECTION]
        inserted = 0
        for d in defaults.iter_default_prompts():
            try:
                if coll.find_one({"_id": d.key}, {"_id": 1}):
                    continue
                coll.insert_one(
                    {
                        "_id": d.key,
                        "scope": d.scope,
                        "name": d.name,
                        "description": d.description,
                        "category": d.category,
                        "content": d.content,
                        "default_content": d.content,
                        "is_overridden": False,
                        "updated_at": datetime.utcnow().isoformat(),
                        "updated_by": None,
                    }
                )
                inserted += 1
            except PyMongoError as error:
                logger.warning("Mongo seed failed for prompt %s: %s", d.key, error)
        if inserted:
            logger.info("Seeded %d default prompt(s) into MongoDB.", inserted)
        return inserted

    # --- admin operations ---------------------------------------------------
    def list_docs(self, scope: str | None = None) -> list[dict]:
        """Return all prompt docs (Mongo overlaid on defaults), newest schema."""
        db = get_mongo_db()
        stored: dict[str, dict] = {}
        if db is not None:
            try:
                query = {"scope": scope} if scope else {}
                for doc in db[COLLECTION].find(query):
                    stored[doc["_id"]] = doc
            except PyMongoError as error:
                logger.warning("Mongo list failed: %s", error)

        out: list[dict] = []
        for d in defaults.iter_default_prompts():
            if scope and d.scope != scope:
                continue
            doc = stored.pop(d.key, None)
            out.append(self._public_view(d.key, doc, d))
        # Any custom keys stored in Mongo without a bundled default.
        for key, doc in stored.items():
            out.append(self._public_view(key, doc, None))
        out.sort(key=lambda item: (item["scope"], item["key"]))
        return out

    def get_doc(self, key: str) -> dict | None:
        """Return the full doc for ``key`` (content + default_content), or None."""
        default = defaults.get_default(key)
        db = get_mongo_db()
        doc = None
        if db is not None:
            try:
                doc = db[COLLECTION].find_one({"_id": key})
            except PyMongoError as error:
                logger.warning("Mongo read failed for prompt %s: %s", key, error)
        if doc is None and default is None:
            return None
        return self._public_view(key, doc, default, include_content=True)

    def update(self, key: str, content: str, updated_by: str | None = None) -> dict:
        """Overwrite the content for ``key``. Requires Mongo."""
        db = get_mongo_db()
        if db is None:
            raise MongoUnavailableError("MongoDB is unavailable; cannot edit prompts.")
        default = defaults.get_default(key)
        default_content = default.content if default else None
        set_fields = {
            "content": content,
            "is_overridden": content != default_content,
            "updated_at": datetime.utcnow().isoformat(),
            "updated_by": updated_by,
        }
        on_insert = {
            "scope": default.scope if default else "custom",
            "name": default.name if default else key,
            "description": default.description if default else "",
            "category": default.category if default else "custom",
            "default_content": default_content,
        }
        try:
            db[COLLECTION].update_one(
                {"_id": key},
                {"$set": set_fields, "$setOnInsert": on_insert},
                upsert=True,
            )
        except PyMongoError as error:
            raise MongoUnavailableError(f"Mongo write failed: {error}") from error
        self.invalidate(key)
        return self.get_doc(key)

    def reset(self, key: str) -> dict | None:
        """Restore ``key`` to its bundled default. Requires Mongo."""
        db = get_mongo_db()
        if db is None:
            raise MongoUnavailableError("MongoDB is unavailable; cannot reset prompts.")
        default = defaults.get_default(key)
        try:
            if default is None:
                # No bundled default — drop the custom override entirely.
                db[COLLECTION].delete_one({"_id": key})
            else:
                db[COLLECTION].update_one(
                    {"_id": key},
                    {
                        "$set": {
                            "content": default.content,
                            "default_content": default.content,
                            "is_overridden": False,
                            "updated_at": datetime.utcnow().isoformat(),
                            "updated_by": None,
                        }
                    },
                    upsert=True,
                )
        except PyMongoError as error:
            raise MongoUnavailableError(f"Mongo write failed: {error}") from error
        self.invalidate(key)
        return self.get_doc(key)

    # --- helpers ------------------------------------------------------------
    @staticmethod
    def _public_view(
        key: str,
        doc: dict | None,
        default: "defaults.PromptDefault | None",
        include_content: bool = False,
    ) -> dict:
        default_content = (
            doc.get("default_content")
            if doc and doc.get("default_content") is not None
            else (default.content if default else None)
        )
        content = doc.get("content") if doc else (default.content if default else "")
        view = {
            "key": key,
            "scope": (doc or {}).get("scope") or (default.scope if default else "custom"),
            "name": (doc or {}).get("name") or (default.name if default else key),
            "description": (doc or {}).get("description")
            or (default.description if default else ""),
            "category": (doc or {}).get("category")
            or (default.category if default else "custom"),
            "is_overridden": bool(doc.get("is_overridden")) if doc else False,
            "updated_at": (doc or {}).get("updated_at"),
            "updated_by": (doc or {}).get("updated_by"),
            "has_default": default is not None,
        }
        if include_content:
            view["content"] = content
            view["default_content"] = default_content
        else:
            view["preview"] = (content or "")[:160]
        return view


# Process-wide singleton (mirrors get_code_generation_service / factory style).
prompt_store = PromptStore()
