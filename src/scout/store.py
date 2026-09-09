"""Durable per-anchor baselines and notification outbox."""

import json
import sqlite3
import time
from contextlib import contextmanager, nullcontext
from dataclasses import asdict

from .migrations import marketplace_only
from .models import SearchResult, Watch, matches

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS watches(id INTEGER PRIMARY KEY AUTOINCREMENT, spec TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scans(
 watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
 source TEXT NOT NULL, country TEXT NOT NULL, anchor TEXT NOT NULL,
 next_run REAL NOT NULL DEFAULT 0, initialized INTEGER NOT NULL DEFAULT 0,
 failures INTEGER NOT NULL DEFAULT 0, last_success REAL, error TEXT,
 saturated INTEGER NOT NULL DEFAULT 0, result_count INTEGER,
 PRIMARY KEY(watch_id,source,country,anchor));
CREATE TABLE IF NOT EXISTS seen(
 watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
 source TEXT NOT NULL, listing_id TEXT NOT NULL, first_seen REAL NOT NULL,
 PRIMARY KEY(watch_id,source,listing_id));
CREATE TABLE IF NOT EXISTS outbox(
 id INTEGER PRIMARY KEY, watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
 payload TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 next_try REAL NOT NULL DEFAULT 0, sent_at REAL, error TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, path):
        self.path = path
        with self.connect() as db:
            db.executescript(SCHEMA)
            columns = {r[1] for r in db.execute("PRAGMA table_info(scans)")}
            for name, kind in (("radius_km", "INTEGER"), ("coverage_warning", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE scans ADD COLUMN {name} {kind}")
        path.chmod(0o600)
        marketplace_only(self)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, watch: Watch, regions: dict, connection=None) -> int:
        with nullcontext(connection) if connection is not None else self.connect() as db:
            if connection is None:
                db.execute("BEGIN IMMEDIATE")
            # 24 regional searches/hour per all-country FB watch at the default interval.
            cost = sum(len(regions[c]) for c in watch.countries) * 60 / watch.interval_minutes
            existing = sum(
                sum(len(regions[c]) for c in w.countries) * 60 / w.interval_minutes
                for w in (
                    Watch.model_validate_json(r[0]) for r in db.execute("SELECT spec FROM watches")
                )
                if w.enabled
            )
            if existing + cost > 100:
                raise ValueError("Capacity is 100 regional searches/hour; use a longer interval")
            ident = db.execute(
                "INSERT INTO watches(spec) VALUES (?)", (watch.model_dump_json(),)
            ).lastrowid
            for source in watch.sources:
                for country in watch.countries:
                    anchors = [a["city"] for a in regions[country]]
                    for anchor in anchors:
                        db.execute(
                            "INSERT INTO scans(watch_id,source,country,anchor) VALUES (?,?,?,?)",
                            (ident, source, country, anchor),
                        )
            return ident

    def watches(self):
        with self.connect() as db:
            return [
                {"id": r["id"], **json.loads(r["spec"])}
                for r in db.execute("SELECT * FROM watches ORDER BY id")
            ]

    def remove(self, ident):
        with self.connect() as db:
            return db.execute("DELETE FROM watches WHERE id=?", (ident,)).rowcount > 0

    def due(self, now):
        with self.connect() as db:
            rows = db.execute(
                """SELECT scans.*,spec FROM scans JOIN watches ON watches.id=watch_id
                WHERE next_run<=? ORDER BY next_run,watch_id,country,anchor""",
                (now,),
            ).fetchall()
        return [dict(r) for r in rows if Watch.model_validate_json(r["spec"]).enabled]

    @staticmethod
    def key(scan):
        return tuple(scan[k] for k in ("watch_id", "source", "country", "anchor"))

    def record(self, scan, result: SearchResult, now):
        watch = Watch.model_validate_json(scan["spec"])
        key = self.key(scan)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT initialized FROM scans WHERE watch_id=? AND source=? AND country=? AND anchor=?",
                key,
            ).fetchone()
            if row is None:  # Watch deleted while search was in flight.
                return
            initial = not row[0]
            # Limit initial alerts to ten per watch, across countries and anchors.
            initial_key = f"initial:{scan['watch_id']}"
            count_row = db.execute("SELECT value FROM meta WHERE key=?", (initial_key,)).fetchone()
            initial_count = int(count_row[0]) if count_row else 0
            for listing in result.listings:
                if not matches(watch, listing):
                    continue
                fresh = db.execute(
                    "INSERT OR IGNORE INTO seen VALUES (?,?,?,?)",
                    (scan["watch_id"], listing.source, listing.id, now),
                ).rowcount
                if fresh and (not initial or initial_count < 10 or watch.image_profile):
                    payload = {"watch": watch.name, "initial": initial, **asdict(listing)}
                    db.execute(
                        "INSERT INTO outbox(watch_id,payload) VALUES (?,?)",
                        (scan["watch_id"], json.dumps(payload)),
                    )
                    if initial:
                        initial_count += 1
            db.execute(
                "INSERT OR REPLACE INTO meta VALUES (?,?)", (initial_key, str(initial_count))
            )
            db.execute(
                """UPDATE scans SET initialized=1,failures=0,error=NULL,last_success=?,
                next_run=?,saturated=?,result_count=?,radius_km=?,coverage_warning=? WHERE watch_id=? AND source=? AND country=? AND anchor=?""",
                (
                    now,
                    now + watch.interval_minutes * 60,
                    int(result.saturated),
                    result.collected_count
                    if result.collected_count is not None
                    else len(result.listings),
                    result.radius_km,
                    result.coverage_warning,
                    *key,
                ),
            )

    def failed(self, scan, error, now):
        delay = min(86400, 1800 * 2 ** min(scan["failures"], 6))
        with self.connect() as db:
            db.execute(
                """UPDATE scans SET failures=failures+1,error=?,next_run=?
                WHERE watch_id=? AND source=? AND country=? AND anchor=?""",
                (error, now + delay, *self.key(scan)),
            )

    def health(self):
        with self.connect() as db:
            return {
                "scans": [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM scans ORDER BY watch_id,source,country,anchor"
                    )
                ],
                "pending_alerts": db.execute(
                    "SELECT count(*) FROM outbox WHERE sent_at IS NULL"
                ).fetchone()[0],
                "failed_alerts": db.execute(
                    "SELECT count(*) FROM outbox WHERE sent_at IS NULL AND attempts>0"
                ).fetchone()[0],
                "sent_alerts": db.execute(
                    "SELECT count(*) FROM outbox WHERE sent_at IS NOT NULL"
                ).fetchone()[0],
                "worker_heartbeat": (
                    db.execute("SELECT value FROM meta WHERE key='heartbeat'").fetchone() or [None]
                )[0],
            }

    def heartbeat(self):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('heartbeat',?)", (str(time.time()),))

    def resume_source(self, source):
        """Clear access backoff after successful manual login; preserve baselines and seen IDs."""
        with self.connect() as db:
            db.execute("DELETE FROM meta WHERE key=?", (f"cooldown:{source}",))
            db.execute(
                "UPDATE scans SET next_run=0, failures=0, error=NULL WHERE source=?", (source,)
            )
