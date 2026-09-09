"""Cached photo checks for notifications; uncertain candidates stay reviewable."""

import asyncio
import hashlib
import json
import os
import sys
import time
from dataclasses import asdict
from urllib.parse import urlsplit

import httpx

from .image_profiles import get_profile
from .models import SearchResult, matches

POLICY = "sift-v1"
SCHEMA = """
CREATE TABLE IF NOT EXISTS visual_checks(
 watch_id INTEGER NOT NULL REFERENCES watches(id) ON DELETE CASCADE,
 source TEXT NOT NULL, listing_id TEXT NOT NULL, image_key TEXT NOT NULL,
 policy TEXT NOT NULL, decision TEXT NOT NULL, score INTEGER NOT NULL DEFAULT 0,
 reason TEXT NOT NULL, checked_at REAL NOT NULL, retry_at REAL NOT NULL DEFAULT 0,
 payload TEXT NOT NULL, PRIMARY KEY(watch_id,source,listing_id));
CREATE TABLE IF NOT EXISTS visual_budget(id INTEGER PRIMARY KEY, checked_at REAL NOT NULL);
"""


def image_key(url):
    parsed = urlsplit(url or "")
    if parsed.scheme != "https" or not any(
        parsed.hostname == host or (parsed.hostname or "").endswith("." + host)
        for host in ("fbcdn.net",)
    ):
        return None
    # Signed thumbnail URLs rotate; the photo's asset path is the stable identity.
    return hashlib.sha256(parsed.path.encode()).hexdigest()


class PhotoFilter:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        with store.connect() as db:
            db.executescript(SCHEMA)

    def save(self, watch_id, listing, key, decision, score, reason, retry_at=0, *, policy=POLICY):
        with self.store.connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO visual_checks VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    watch_id,
                    listing.source,
                    listing.id,
                    key or "",
                    policy,
                    decision,
                    score,
                    reason,
                    time.time(),
                    retry_at,
                    json.dumps(asdict(listing)),
                ),
            )
            db.execute(
                "DELETE FROM visual_checks WHERE watch_id=? AND rowid IN (SELECT rowid FROM visual_checks WHERE watch_id=? ORDER BY checked_at DESC LIMIT -1 OFFSET 2000)",
                (watch_id, watch_id),
            )

    def reserve(self):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM visual_budget WHERE checked_at < ?", (time.time() - 3600,))
            if db.execute("SELECT count(*) FROM visual_budget").fetchone()[0] >= 60:
                return False
            db.execute("INSERT INTO visual_budget(checked_at) VALUES (?)", (time.time(),))
            return True

    async def inspect(self, listing, profile):
        # No redirected URLs or unbounded response buffering.
        async with httpx.AsyncClient(timeout=6, follow_redirects=False) as client:
            async with client.stream("GET", listing.image_url) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 2_000_000:
                        raise ValueError("Image exceeds byte limit")
        env = {
            **os.environ,
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
        }
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "scout.image_match",
            str(profile.directory),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(bytes(data)), 4)
            if proc.returncode != 0:
                raise RuntimeError("Photo comparison failed")
            return json.loads(stdout)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    async def filter(self, scan, spec, result):
        if not spec.image_profile:
            return result
        profile = get_profile(self.settings.reference_root, spec.image_profile)
        accepted, spent = [], 0
        for listing in result.listings:
            if not matches(spec, listing):
                continue
            key = image_key(listing.image_url)
            with self.store.connect() as db:
                seen = db.execute(
                    "SELECT 1 FROM seen WHERE watch_id=? AND source=? AND listing_id=?",
                    (scan["watch_id"], listing.source, listing.id),
                ).fetchone()
                cached = db.execute(
                    "SELECT * FROM visual_checks WHERE watch_id=? AND source=? AND listing_id=?",
                    (scan["watch_id"], listing.source, listing.id),
                ).fetchone()
            if seen:
                continue  # Already delivered/baselined; no image download or CPU work.
            if profile is None:
                self.save(
                    scan["watch_id"],
                    listing,
                    key,
                    "review",
                    0,
                    "Photo references unavailable",
                    time.time() + 3600,
                )
                continue
            if (
                cached
                and cached["policy"] == profile.fingerprint
                and cached["image_key"] == (key or "")
            ):
                if cached["decision"] in ("match", "approved"):
                    accepted.append(listing)
                    continue
                if cached["retry_at"] > time.time():
                    continue
            if not key:
                self.save(
                    scan["watch_id"],
                    listing,
                    key,
                    "review",
                    0,
                    "Photo unavailable",
                    time.time() + 3600,
                    policy=profile.fingerprint,
                )
                continue
            if spent >= 8 or not self.reserve():
                self.save(
                    scan["watch_id"],
                    listing,
                    key,
                    "deferred",
                    0,
                    "Waiting for photo-check budget",
                    time.time() + 300,
                    policy=profile.fingerprint,
                )
                continue
            spent += 1
            try:
                async with asyncio.timeout(12):
                    score = await self.inspect(listing, profile)
                decision = score["decision"]
                self.save(
                    scan["watch_id"],
                    listing,
                    key,
                    decision,
                    score["inliers"],
                    "Photo resembles reference"
                    if decision == "match"
                    else "Photo not confidently matched",
                    time.time() + 7 * 86400 if decision == "review" else 0,
                    policy=profile.fingerprint,
                )
                if decision == "match":
                    accepted.append(listing)
            except Exception:
                self.save(
                    scan["watch_id"],
                    listing,
                    key,
                    "review",
                    0,
                    "Photo check unavailable; retry later",
                    time.time() + 3600,
                    policy=profile.fingerprint,
                )
        return SearchResult(
            accepted,
            result.saturated,
            result.radius_km,
            result.coverage_warning,
            len(result.listings),
        )

    def review(self, watch_id, offset=0, limit=60):
        with self.store.connect() as db:
            where = "watch_id=? AND decision IN ('review','deferred')"
            total = db.execute(
                f"SELECT count(*) FROM visual_checks WHERE {where}", (watch_id,)
            ).fetchone()[0]
            rows = db.execute(
                f"SELECT * FROM visual_checks WHERE {where} ORDER BY score DESC,checked_at DESC LIMIT ? OFFSET ?",
                (watch_id, limit, offset),
            ).fetchall()
        return {
            "total": total,
            "items": [
                {**json.loads(r["payload"]), "reason": r["reason"], "score": r["score"]}
                for r in rows
            ],
        }

    def approve(self, watch_id, source, listing_id):
        from .models import Listing, Watch

        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM visual_checks WHERE watch_id=? AND source=? AND listing_id=?",
                (watch_id, source, listing_id),
            ).fetchone()
            if not row:
                raise KeyError(listing_id)
            watch = Watch.model_validate_json(
                db.execute("SELECT spec FROM watches WHERE id=?", (watch_id,)).fetchone()[0]
            )
            listing = json.loads(row["payload"])
            if not watch.enabled or not matches(watch, Listing(**listing)):
                raise ValueError(
                    "Watch is paused or the listing no longer passes its title filters"
                )
            existing = db.execute(
                "SELECT id FROM outbox WHERE watch_id=? AND json_extract(payload,'$.source')=? AND json_extract(payload,'$.id')=?",
                (watch_id, source, listing_id),
            ).fetchone()
            if not existing:
                db.execute(
                    "INSERT OR IGNORE INTO seen VALUES (?,?,?,?)",
                    (watch_id, source, listing_id, time.time()),
                )
                db.execute(
                    "INSERT INTO outbox(watch_id,payload) VALUES (?,?)",
                    (watch_id, json.dumps({"watch": watch.name, "initial": False, **listing})),
                )
            db.execute(
                "UPDATE visual_checks SET decision='approved' WHERE watch_id=? AND source=? AND listing_id=?",
                (watch_id, source, listing_id),
            )
