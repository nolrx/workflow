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
import hashlib
import logging
import threading
import time
from datetime import datetime

from pymongo.errors import PyMongoError

from backend.services.mongo import get_mongo_db
from backend.services.prompts import defaults

logger = logging.getLogger(__name__)

COLLECTION = "prompts"
# Immutable per-edit history. Each doc ``_id = "<key>@<content_hash>"`` is written
# once and never mutated, so a published graph pinned to a hash always resolves to
# the exact same prompt body — independent of later edits to the live HEAD.
VERSIONS_COLLECTION = "prompt_versions"
_CACHE_TTL_SECONDS = 60.0


def content_hash(content: str) -> str:
    """Stable content-addressed id for a prompt body (the pin identity)."""
    return "sha256:" + hashlib.sha256((content or "").encode("utf-8")).hexdigest()


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

    # --- version pinning ----------------------------------------------------
    # The HEAD read path above is unchanged: ``get`` still returns the live
    # content. These methods add an immutable version layer so a published graph
    # can freeze the EXACT prompt it ran with (reproducible replay).
    @staticmethod
    def _write_version(db, key, content, c_hash, *, parent_hash, version, created_by) -> None:
        """Append an immutable version doc (idempotent on hash; never overwrites)."""
        try:
            db[VERSIONS_COLLECTION].update_one(
                {"_id": f"{key}@{c_hash}"},
                {
                    "$setOnInsert": {
                        "key": key,
                        "version": version,
                        "content": content,
                        "content_hash": c_hash,
                        "parent_hash": parent_hash,
                        "created_at": datetime.utcnow().isoformat(),
                        "created_by": created_by,
                    }
                },
                upsert=True,
            )
        except PyMongoError as error:
            logger.warning("Mongo version write failed for %s: %s", key, error)

    def head_pin(self, key: str) -> dict:
        """Return a pin ``{key, version, hash}`` for the current HEAD content.

        Called when a canvas/graph is published, to freeze the exact prompt. An
        override is materialized as an immutable version so the pin survives
        later edits; default-equal content needs no version doc (``get_pinned``
        resolves it from the bundled default). Works (best-effort) when Mongo is
        down — it pins to the default body, which the fallback can still resolve.
        """
        content = self.get(key)  # HEAD (raises KeyError on an unknown key)
        h = content_hash(content)
        db = get_mongo_db()
        if db is None:
            return {"key": key, "version": 0, "hash": h}
        try:
            head = db[COLLECTION].find_one({"_id": key}, {"version": 1, "content_hash": 1})
        except PyMongoError:
            return {"key": key, "version": 0, "hash": h}
        version = (head or {}).get("version") or 0
        if (head or {}).get("content_hash") == h and version:
            return {"key": key, "version": version, "hash": h}
        if content != defaults.get_default_content(key):
            version = (version + 1) if version else 1
            self._write_version(
                db, key, content, h,
                parent_hash=(head or {}).get("content_hash"), version=version, created_by=None,
            )
            try:
                db[COLLECTION].update_one(
                    {"_id": key}, {"$set": {"version": version, "content_hash": h}}
                )
            except PyMongoError:
                pass
            return {"key": key, "version": version, "hash": h}
        return {"key": key, "version": 0, "hash": h}

    def get_pinned(self, key: str, prompt_hash: str) -> str:
        """Resolve the EXACT pinned content (content-addressed). Replay-safe.

        Resolution order: immutable version doc → bundled-default fallback (when
        the pin is to the default body) → ``KeyError``. Independent of the live
        HEAD, so editing a prompt never changes what a published graph runs.
        """
        db = get_mongo_db()
        if db is not None:
            try:
                doc = db[VERSIONS_COLLECTION].find_one(
                    {"_id": f"{key}@{prompt_hash}"}, {"content": 1}
                )
                if doc and doc.get("content") is not None:
                    return doc["content"]
            except PyMongoError as error:
                logger.warning("Mongo pinned read failed for %s: %s", key, error)
        default = defaults.get_default_content(key)
        if default is not None and content_hash(default) == prompt_hash:
            return default
        raise KeyError(f"Pinned prompt not found: {key}@{prompt_hash}")

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
        # Version lineage: read the current HEAD's version/hash so a content change
        # bumps the version and records an immutable history entry.
        try:
            head = db[COLLECTION].find_one({"_id": key}, {"version": 1, "content_hash": 1})
        except PyMongoError:
            head = None
        prev_hash = (head or {}).get("content_hash")
        prev_version = (head or {}).get("version") or 0
        new_hash = content_hash(content)
        changed = new_hash != prev_hash
        new_version = (prev_version + 1) if changed else (prev_version or 1)
        set_fields = {
            "content": content,
            "is_overridden": content != default_content,
            "updated_at": datetime.utcnow().isoformat(),
            "updated_by": updated_by,
            "version": new_version,
            "content_hash": new_hash,
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
        if changed or prev_version == 0:
            self._write_version(
                db, key, content, new_hash,
                parent_hash=prev_hash, version=new_version, created_by=updated_by,
            )
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
