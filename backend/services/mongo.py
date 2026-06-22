"""
MongoDB middleware.

A thin, thread-safe accessor for the MongoDB database used to store editable
system prompts (see ``backend/services/prompts``). Modeled on
``backend/services/ai/factory.py``: it reads connection settings directly from
``os.getenv`` (not Flask app config) so it works in any thread / context,
caches a single client behind a lock, and **fails soft** — if Mongo is not
configured or is unreachable, ``get_mongo_db()`` returns ``None`` and callers
fall back to bundled defaults rather than crashing.

Environment variables:
    MONGODB_URI   connection string (default ``mongodb://localhost:27017``)
    MONGODB_DB    database name      (default ``ai_creative_studio``)
"""
import logging
import os
import threading

from pymongo import MongoClient
from pymongo.database import Database
from pymongo.errors import PyMongoError

logger = logging.getLogger(__name__)

# Server-selection timeout for the one-time reachability probe. Kept modest so an
# unreachable Mongo fails fast and callers fall back to bundled defaults — but not
# so short that a real-but-slow first connect (e.g. macOS Docker Desktop / vpnkit
# cold start, which can take a few seconds) is wrongly judged unavailable. The
# probe result is cached per process, so this is paid at most once. Tunable via
# MONGODB_SERVER_SELECTION_TIMEOUT_MS.
def _server_selection_timeout_ms() -> int:
    try:
        return max(500, int(os.getenv("MONGODB_SERVER_SELECTION_TIMEOUT_MS", "3000")))
    except (TypeError, ValueError):
        return 3000

_client: MongoClient | None = None
_db: Database | None = None
_probed_unavailable = False
_lock = threading.Lock()


def _default_uri() -> str:
    return os.getenv("MONGODB_URI", "mongodb://localhost:27017")


def _default_db_name() -> str:
    return os.getenv("MONGODB_DB", "ai_creative_studio")


def get_mongo_db(force_new: bool = False) -> Database | None:
    """Return the shared Mongo database, or ``None`` if unavailable.

    The first call probes the server with a short timeout. If the probe fails
    the result is cached (``_probed_unavailable``) so subsequent calls return
    ``None`` immediately without re-incurring the timeout — until
    ``reset_mongo()`` is called (e.g. after the server comes back, or in tests).

    Args:
        force_new: build a brand-new client instead of reusing the cached one.
                   Use in long-lived background threads if desired; the default
                   client is already thread-safe so this is rarely needed.
    """
    global _client, _db, _probed_unavailable

    if not force_new:
        if _db is not None:
            return _db
        if _probed_unavailable:
            return None

    with _lock:
        if not force_new:
            if _db is not None:
                return _db
            if _probed_unavailable:
                return None

        uri = _default_uri()
        db_name = _default_db_name()
        try:
            client: MongoClient = MongoClient(
                uri, serverSelectionTimeoutMS=_server_selection_timeout_ms()
            )
            # Force a round-trip so we know the server is actually reachable.
            client.admin.command("ping")
        except PyMongoError as error:
            logger.warning(
                "MongoDB unavailable at %s (%s); falling back to bundled defaults.",
                uri,
                error,
            )
            if not force_new:
                _probed_unavailable = True
            return None

        db = client[db_name]
        if not force_new:
            _client = client
            _db = db
            _probed_unavailable = False
        logger.info("MongoDB connected: %s / %s", uri, db_name)
        return db


def is_available() -> bool:
    """Return True if MongoDB is currently reachable."""
    return get_mongo_db() is not None


def reset_mongo() -> None:
    """Drop the cached client/db so the next call re-probes the server.

    Used by tests (after changing ``MONGODB_*`` env vars) and anywhere a fresh
    connection probe is wanted.
    """
    global _client, _db, _probed_unavailable
    with _lock:
        if _client is not None:
            try:
                _client.close()
            except PyMongoError:
                pass
        _client = None
        _db = None
        _probed_unavailable = False
