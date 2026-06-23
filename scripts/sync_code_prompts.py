#!/usr/bin/env python3
"""
Sync rewritten Code-domain prompt defaults (.txt) into the MongoDB ``prompts``
collection so they actually take effect at runtime.

Why this exists
---------------
``prompt_store.get(key)`` serves the Mongo document's ``content`` when present,
and ``seed_defaults()`` only *inserts missing* keys — it never overwrites an
existing document (to protect admin edits). So editing ``backend/prompts/code/*.txt``
alone does NOT change what the running app serves for keys that were already
seeded. This script pushes the new ``.txt`` content into Mongo.

Admin-override safety
---------------------
A document with ``is_overridden=True`` was edited by an admin via the prompt
console. By default this script **skips** updating such a document's ``content``
(it only refreshes ``default_content`` so "reset to default" still yields the new
BMAD default). Pass ``--force`` to overwrite admin-overridden content too.

Takes effect within the prompt store's 60s cache TTL — no backend restart needed.

Usage
-----
    uv run python scripts/sync_code_prompts.py                # sync all code/* (skip admin-overridden content)
    uv run python scripts/sync_code_prompts.py --dry-run      # show what would change, write nothing
    uv run python scripts/sync_code_prompts.py --force        # also overwrite admin-overridden content
    uv run python scripts/sync_code_prompts.py --key code/requirements_prompt.txt   # one key

Env: MONGODB_URI (default mongodb://localhost:27017), MONGODB_DB (default ai_creative_studio).
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

# Make ``backend`` importable when run as a plain script from the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.services.mongo import get_mongo_db  # noqa: E402
from backend.services.prompts import defaults  # noqa: E402

COLLECTION = "prompts"


def _code_defaults(only_key: str | None) -> list:
    out = []
    for d in defaults.iter_default_prompts():
        if d.scope != "code":
            continue
        if only_key and d.key != only_key:
            continue
        out.append(d)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync code/* prompt defaults into MongoDB.")
    parser.add_argument("--dry-run", action="store_true", help="show changes without writing")
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite content even for admin-overridden (is_overridden=True) docs",
    )
    parser.add_argument("--key", default=None, help="only sync this exact key (e.g. code/style_prompt.txt)")
    args = parser.parse_args()

    db = get_mongo_db()
    if db is None:
        print(
            "✗ MongoDB unavailable (check MONGODB_URI / MONGODB_DB). Nothing synced.\n"
            "  The app will keep serving bundled .txt defaults only where Mongo has no document.",
            file=sys.stderr,
        )
        return 2

    coll = db[COLLECTION]
    items = _code_defaults(args.key)
    if not items:
        print("No matching code/* defaults found.", file=sys.stderr)
        return 1

    inserted = updated = skipped = unchanged = 0
    for d in items:
        existing = coll.find_one({"_id": d.key})
        now = datetime.utcnow().isoformat()

        if existing is None:
            action = "INSERT"
            if not args.dry_run:
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
                        "updated_at": now,
                        "updated_by": "sync_code_prompts",
                    }
                )
            inserted += 1
            print(f"  [{action}] {d.key}")
            continue

        is_overridden = bool(existing.get("is_overridden"))
        cur_content = existing.get("content")
        cur_default = existing.get("default_content")

        if is_overridden and not args.force:
            # Keep admin content; just refresh the recorded default so a future
            # "reset to default" yields the new BMAD version.
            set_fields = {}
            if cur_default != d.content:
                set_fields["default_content"] = d.content
                set_fields["updated_at"] = now
            if set_fields and not args.dry_run:
                coll.update_one({"_id": d.key}, {"$set": set_fields})
            skipped += 1
            note = "default_content refreshed" if set_fields else "no change"
            print(f"  [SKIP-OVERRIDDEN] {d.key}  (admin content kept; {note}) — use --force to overwrite")
            continue

        if cur_content == d.content and cur_default == d.content and not is_overridden:
            unchanged += 1
            print(f"  [unchanged] {d.key}")
            continue

        action = "UPDATE-FORCE" if (is_overridden and args.force) else "UPDATE"
        if not args.dry_run:
            coll.update_one(
                {"_id": d.key},
                {
                    "$set": {
                        "content": d.content,
                        "default_content": d.content,
                        "is_overridden": False,
                        "updated_at": now,
                        "updated_by": "sync_code_prompts",
                    }
                },
            )
        updated += 1
        print(f"  [{action}] {d.key}")

    verb = "Would sync" if args.dry_run else "Synced"
    print(
        f"\n{verb}: {inserted} inserted, {updated} updated, "
        f"{skipped} skipped(admin-overridden), {unchanged} unchanged "
        f"(of {len(items)} code/* prompts)."
    )
    if not args.dry_run and (inserted or updated):
        print("Takes effect within ~60s (prompt store cache TTL); no backend restart needed.")
    if skipped and not args.force:
        print("Note: skipped docs were edited by an admin online. Re-run with --force to overwrite them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
