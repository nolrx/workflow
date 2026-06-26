"""
Unit tests for prompt version pinning.

Two paths are covered without a real MongoDB (the suite forces Mongo unavailable,
see conftest):
  * the default-pin round-trip, which must resolve with ZERO Mongo writes (most
    prompts are never edited → they pin to the bundled default); and
  * the Mongo version-lineage path, exercised against a tiny in-memory fake so
    ``update`` → immutable history → ``get_pinned`` is genuinely tested.
"""
import pytest

from backend.services.prompts import content_hash, prompt_store

REQ_KEY = "code/requirements_prompt.txt"


# --- pure + default-fallback path (no Mongo) ----------------------------------
def test_content_hash_is_stable_and_prefixed():
    assert content_hash("abc") == content_hash("abc")
    assert content_hash("abc") != content_hash("abd")
    assert content_hash("abc").startswith("sha256:")


def test_default_pin_resolves_without_mongo():
    prompt_store.reset_cache()
    content = prompt_store.get(REQ_KEY)  # bundled default (Mongo down in tests)
    pin = prompt_store.head_pin(REQ_KEY)
    assert pin["key"] == REQ_KEY
    assert pin["hash"] == content_hash(content)
    # A pin to the default body resolves with no version doc written.
    assert prompt_store.get_pinned(REQ_KEY, pin["hash"]) == content


def test_get_pinned_unknown_hash_raises():
    with pytest.raises(KeyError):
        prompt_store.get_pinned(REQ_KEY, "sha256:deadbeef")


# --- Mongo version-lineage path (fake in-memory db) ---------------------------
class _FakeColl:
    def __init__(self):
        self.docs: dict[str, dict] = {}

    def find_one(self, flt, projection=None):
        doc = self.docs.get(flt.get("_id"))
        return dict(doc) if doc else None

    def update_one(self, flt, update, upsert=False):
        _id = flt.get("_id")
        doc = self.docs.get(_id)
        if doc is None:
            if not upsert:
                return
            doc = {"_id": _id}
            doc.update(update.get("$setOnInsert", {}))
            doc.update(update.get("$set", {}))
            self.docs[_id] = doc
        else:
            doc.update(update.get("$set", {}))  # $setOnInsert ignored on existing


class _FakeDB:
    def __init__(self):
        self.colls: dict[str, _FakeColl] = {}

    def __getitem__(self, name):
        return self.colls.setdefault(name, _FakeColl())


def test_version_lineage_with_fake_mongo(monkeypatch):
    fake = _FakeDB()
    monkeypatch.setattr("backend.services.prompts.store.get_mongo_db", lambda *a, **k: fake)
    prompt_store.reset_cache()

    prompt_store.update(REQ_KEY, "VERSION ONE", updated_by="tester")
    prompt_store.reset_cache()
    pin1 = prompt_store.head_pin(REQ_KEY)
    assert pin1["version"] == 1

    prompt_store.update(REQ_KEY, "VERSION TWO", updated_by="tester")
    prompt_store.reset_cache()
    pin2 = prompt_store.head_pin(REQ_KEY)
    assert pin2["version"] == 2
    assert pin2["hash"] != pin1["hash"]

    # HEAD is V2, but the OLD pin still resolves to V1 (replay-safe).
    assert prompt_store.get(REQ_KEY) == "VERSION TWO"
    assert prompt_store.get_pinned(REQ_KEY, pin1["hash"]) == "VERSION ONE"
    assert prompt_store.get_pinned(REQ_KEY, pin2["hash"]) == "VERSION TWO"


def test_unchanged_update_does_not_bump_version(monkeypatch):
    fake = _FakeDB()
    monkeypatch.setattr("backend.services.prompts.store.get_mongo_db", lambda *a, **k: fake)
    prompt_store.reset_cache()

    prompt_store.update(REQ_KEY, "SAME", updated_by="tester")
    prompt_store.reset_cache()
    v1 = prompt_store.head_pin(REQ_KEY)["version"]
    prompt_store.update(REQ_KEY, "SAME", updated_by="tester")  # no content change
    prompt_store.reset_cache()
    v2 = prompt_store.head_pin(REQ_KEY)["version"]
    assert v1 == v2 == 1
