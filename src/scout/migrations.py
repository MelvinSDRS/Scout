"""One-time retirement of saved eBay state, with a private SQLite backup."""

import json
import os
import sqlite3
import tempfile
from pathlib import Path

MARKETPLACE_ONLY = "migration:marketplace-only-v1"


def marketplace_only(store):
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM meta WHERE key=?", (MARKETPLACE_ONLY,)).fetchone():
            return
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # A separate read connection can back up while this transaction excludes writers.
        if any(
            db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() for table in ("watches", "meta")
        ) or ("searches" in tables and db.execute("SELECT 1 FROM searches LIMIT 1").fetchone()):
            fd, name = tempfile.mkstemp(
                prefix=store.path.name + ".before-marketplace-only-",
                suffix=".sqlite3",
                dir=store.path.parent,
            )
            os.close(fd)
            try:
                with store.connect() as source:
                    backup = sqlite3.connect(name)
                    try:
                        source.backup(backup)
                    finally:
                        backup.close()
            except BaseException:
                Path(name).unlink(missing_ok=True)
                raise

        for table in ("watches", "searches"):
            if table not in tables:
                continue
            for row in db.execute(f"SELECT id,spec FROM {table}").fetchall():
                spec = json.loads(row["spec"])
                if "ebay" not in spec.get("sources", []):
                    continue
                if "facebook" in spec["sources"]:
                    spec["sources"] = ["facebook"]
                    db.execute(
                        f"UPDATE {table} SET spec=? WHERE id=?", (json.dumps(spec), row["id"])
                    )
                else:
                    # Do not turn a retired-source watch into a new Marketplace subscription.
                    db.execute(f"DELETE FROM {table} WHERE id=?", (row["id"],))
                    if table == "watches":
                        db.execute("DELETE FROM meta WHERE key=?", (f"initial:{row['id']}",))
        for table in ("scans", "seen", "search_jobs", "search_listings", "visual_checks"):
            if table in tables:
                db.execute(f"DELETE FROM {table} WHERE source='ebay'")
        db.execute("DELETE FROM outbox WHERE json_extract(payload,'$.source')='ebay'")
        db.execute("DELETE FROM meta WHERE key='cooldown:ebay'")
        db.execute("INSERT INTO meta VALUES (?, '1')", (MARKETPLACE_ONLY,))
