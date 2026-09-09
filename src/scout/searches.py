"""Persistent on-demand searches, regional progress and watch conversion."""

import json
import time
import uuid
from dataclasses import asdict

from .models import SearchSpec, Watch, matches
from .providers.facebook import REGIONS

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches(
 id TEXT PRIMARY KEY, spec TEXT NOT NULL, created_at REAL NOT NULL,
 watch_id INTEGER REFERENCES watches(id) ON DELETE SET NULL);
CREATE TABLE IF NOT EXISTS search_jobs(
 id INTEGER PRIMARY KEY, search_id TEXT NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
 source TEXT NOT NULL, country TEXT NOT NULL, anchor TEXT NOT NULL, position INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'queued', error TEXT, finished_at REAL,
 result_count INTEGER, radius_km INTEGER, coverage_warning TEXT, saturated INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS search_queue ON search_jobs(state,position,id);
CREATE TABLE IF NOT EXISTS search_listings(
 search_id TEXT NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
 source TEXT NOT NULL, country TEXT NOT NULL, listing_id TEXT NOT NULL,
 payload TEXT NOT NULL, matched INTEGER NOT NULL, discovered_at REAL NOT NULL,
 PRIMARY KEY(search_id,source,country,listing_id));
"""


class Searches:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript(SCHEMA)

    def submit(self, spec: SearchSpec):
        ident = uuid.uuid4().hex
        jobs = []
        # Visit one region in each country before the next round of regions.
        groups = [
            (
                source,
                country,
                [r["city"] for r in REGIONS[country]],
            )
            for source in spec.sources
            for country in spec.countries
        ]
        for n in range(max(len(anchors) for _, _, anchors in groups)):
            for source, country, anchors in groups:
                if n < len(anchors):
                    jobs.append((ident, source, country, anchors[n], len(jobs)))
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM search_jobs WHERE state IN ('queued','running') LIMIT 1"
            ).fetchone():
                raise ValueError(
                    "A search is already running. Open it from recent searches, or cancel it first."
                )
            used = db.execute(
                "SELECT count(*) FROM search_jobs JOIN searches ON searches.id=search_id WHERE created_at>?",
                (time.time() - 3600,),
            ).fetchone()[0]
            if used + len(jobs) > 100:
                raise ValueError(
                    "The hourly search budget is used. Try fewer countries or wait before searching again."
                )
            db.execute(
                "INSERT INTO searches(id,spec,created_at) VALUES (?,?,?)",
                (ident, spec.model_dump_json(), time.time()),
            )
            db.executemany(
                "INSERT INTO search_jobs(search_id,source,country,anchor,position) VALUES (?,?,?,?,?)",
                jobs,
            )
            db.execute(
                "DELETE FROM searches WHERE created_at < ? AND id IN (SELECT id FROM searches ORDER BY created_at DESC LIMIT -1 OFFSET 20)",
                (time.time() - 3600,),
            )
        return ident

    def recover(self):
        # Called only by the process holding the global browser/worker lock.
        with self.store.connect() as db:
            db.execute("UPDATE search_jobs SET state='queued' WHERE state='running'")

    def claim(self):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT search_jobs.*,spec FROM search_jobs JOIN searches ON searches.id=search_id WHERE state='queued' ORDER BY created_at,position LIMIT 1"
            ).fetchone()
            if not row:
                return None
            db.execute("UPDATE search_jobs SET state='running' WHERE id=?", (row["id"],))
            return dict(row)

    def record(self, job, result):
        spec = SearchSpec.model_validate_json(job["spec"])
        now = time.time()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM search_jobs WHERE id=?", (job["id"],)).fetchone()
            if not row or row["state"] != "running":
                return  # Cancellation while the browser was in flight.
            for listing in result.listings:
                db.execute(
                    "INSERT OR IGNORE INTO search_listings VALUES (?,?,?,?,?,?,?)",
                    (
                        job["search_id"],
                        listing.source,
                        job["country"],
                        listing.id,
                        json.dumps(asdict(listing)),
                        int(matches(spec, listing)),
                        now,
                    ),
                )
            db.execute(
                "UPDATE search_jobs SET state='done',finished_at=?,result_count=?,radius_km=?,coverage_warning=?,saturated=? WHERE id=?",
                (
                    now,
                    len(result.listings),
                    result.radius_km,
                    result.coverage_warning,
                    int(result.saturated),
                    job["id"],
                ),
            )

    def fail(self, job, error, source_blocked=False):
        with self.store.connect() as db:
            if source_blocked:
                db.execute(
                    "UPDATE search_jobs SET state='failed',error=?,finished_at=? WHERE search_id=? AND source=? AND state IN ('queued','running')",
                    (error, time.time(), job["search_id"], job["source"]),
                )
            else:
                db.execute(
                    "UPDATE search_jobs SET state='failed',error=?,finished_at=? WHERE id=? AND state='running'",
                    (error, time.time(), job["id"]),
                )

    def cancel(self, ident):
        with self.store.connect() as db:
            if not db.execute("SELECT 1 FROM searches WHERE id=?", (ident,)).fetchone():
                return False
            db.execute(
                "UPDATE search_jobs SET state='cancelled' WHERE search_id=? AND state IN ('queued','running')",
                (ident,),
            )
            return True

    def recent(self):
        with self.store.connect() as db:
            ids = [
                r[0]
                for r in db.execute("SELECT id FROM searches ORDER BY created_at DESC LIMIT 20")
            ]
        return [self.get(ident) for ident in ids]

    def get(self, ident):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM searches WHERE id=?", (ident,)).fetchone()
            if not row:
                return None
            jobs = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM search_jobs WHERE search_id=? ORDER BY position", (ident,)
                )
            ]
            counts = {
                r["country"]: dict(r)
                for r in db.execute(
                    "SELECT country,count(*) listings,sum(matched) matches FROM search_listings WHERE search_id=? GROUP BY country",
                    (ident,),
                )
            }
        spec = json.loads(row["spec"])
        countries = []
        for country in spec["countries"]:
            regions = [j for j in jobs if j["country"] == country]
            for region in regions:
                region["name"] = next(
                    (r["name"] for r in REGIONS[country] if r["city"] == region["anchor"]),
                    "Country search",
                )
            countries.append(
                {
                    "country": country,
                    "total": len(regions),
                    "done": sum(j["state"] == "done" for j in regions),
                    "failed": sum(j["state"] == "failed" for j in regions),
                    "running": any(j["state"] == "running" for j in regions),
                    "cancelled": sum(j["state"] == "cancelled" for j in regions),
                    "listings": counts.get(country, {}).get("listings", 0),
                    "matches": counts.get(country, {}).get("matches", 0),
                    "regions": regions,
                }
            )
        states = {j["state"] for j in jobs}
        status = (
            "running"
            if states & {"queued", "running"}
            else "cancelled"
            if "cancelled" in states
            else "partial"
            if "failed" in states and "done" in states
            else "failed"
            if "failed" in states
            else "complete"
        )
        return {
            "id": ident,
            "spec": spec,
            "created_at": row["created_at"],
            "watch_id": row["watch_id"],
            "status": status,
            "total_regions": len(jobs),
            "finished_regions": sum(j["state"] in ("done", "failed", "cancelled") for j in jobs),
            "countries": countries,
        }

    def results(self, ident, country, suggestions=False, offset=0, limit=60):
        with self.store.connect() as db:
            where = "search_id=? AND country=?" + ("" if suggestions else " AND matched=1")
            total = db.execute(
                f"SELECT count(*) FROM search_listings WHERE {where}", (ident, country)
            ).fetchone()[0]
            rows = db.execute(
                f"SELECT payload,matched FROM search_listings WHERE {where} ORDER BY matched DESC, discovered_at,listing_id LIMIT ? OFFSET ?",
                (ident, country, limit, offset),
            ).fetchall()
        return {
            "total": total,
            "offset": offset,
            "items": [{**json.loads(r["payload"]), "matched": bool(r["matched"])} for r in rows],
        }

    def create_watch(self, ident, name=None, interval=60):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM searches WHERE id=?", (ident,)).fetchone()
            if not row:
                raise KeyError(ident)
            if row["watch_id"] is not None:
                return row["watch_id"]
            spec = SearchSpec.model_validate_json(row["spec"])
            watch = Watch(
                **spec.model_dump(), name=name or spec.query[:100], interval_minutes=interval
            )
            watch_id = self.store.add(watch, REGIONS, connection=db)
            # Existing displayed matches are the baseline, not new-item notifications.
            db.execute(
                "INSERT OR IGNORE INTO seen SELECT ?,source,listing_id,min(discovered_at) FROM search_listings WHERE search_id=? AND matched=1 GROUP BY source,listing_id",
                (watch_id, ident),
            )
            for job in db.execute(
                "SELECT * FROM search_jobs WHERE search_id=? AND state='done'", (ident,)
            ).fetchall():
                db.execute(
                    "UPDATE scans SET initialized=1,last_success=?,next_run=?,result_count=?,radius_km=?,coverage_warning=?,saturated=? WHERE watch_id=? AND source=? AND country=? AND anchor=?",
                    (
                        job["finished_at"],
                        job["finished_at"] + interval * 60,
                        job["result_count"],
                        job["radius_km"],
                        job["coverage_warning"],
                        job["saturated"],
                        watch_id,
                        job["source"],
                        job["country"],
                        job["anchor"],
                    ),
                )
            db.execute("UPDATE searches SET watch_id=? WHERE id=?", (watch_id, ident))
            return watch_id
