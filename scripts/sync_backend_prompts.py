"""One-off: push the two backend-project prompts from the local .txt into Mongo.

Run on the HOST (the running backend container ships an older baked copy of the
.txt, so its `defaults` would seed stale content). Mongo is reached via
MONGODB_URI (compose publishes it on localhost:27017). Skips a key that an admin
has hand-edited online (is_overridden=True) so we never clobber a manual change.
"""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")
os.environ.setdefault("MONGODB_DB", "ai_creative_studio")

from backend.services.mongo import get_mongo_db  # noqa: E402
from backend.services.prompts import defaults  # noqa: E402
from backend.services.prompts.store import prompt_store  # noqa: E402

KEYS = [
    "code/backend_project_prompt.txt",
    "code/backend_project_reinforce_prompt.txt",
    "code/backend_project_repair_prompt.txt",
    "code/backend_project_critic_prompt.txt",
]

db = get_mongo_db()
if db is None:
    raise SystemExit("✗ Mongo 不可达(检查 MONGODB_URI / mongo 容器)")

for key in KEYS:
    new_content = defaults.get_default_content(key)  # reads the local (edited) .txt
    if not new_content:
        print(f"✗ {key}: 本地无默认内容,跳过")
        continue
    doc = prompt_store.get_doc(key) or {}
    overridden = bool(doc.get("is_overridden"))
    stored = doc.get("content")
    if stored == new_content:
        print(f"= {key}: Mongo 内容已与本地一致,无需更新 ({len(new_content)} 字符)")
        continue
    if overridden:
        print(f"! {key}: is_overridden=True(疑似 admin 手改),未覆盖。如确需同步请手动确认。")
        continue
    prompt_store.update(key, new_content, updated_by="sync_backend_prompts.py")
    back = get_mongo_db()[ "prompts" ].find_one({"_id": key}, {"content": 1})
    ok = bool(back) and back.get("content") == new_content
    print(f"✓ {key}: 已写入 Mongo({len(new_content)} 字符),回读校验={'OK' if ok else 'FAIL'}")

print("done.")
