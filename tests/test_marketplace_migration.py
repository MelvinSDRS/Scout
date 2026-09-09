import json
import sqlite3
from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from scout.api import create_app
from scout.image_filter import PhotoFilter, image_key
from scout.migrations import MARKETPLACE_ONLY
from scout.models import Listing, SearchResult, SearchSpec, Watch
from scout.providers.facebook import REGIONS
from scout.searches import Searches
from scout.settings import Settings
from scout.store import Store


@pytest.mark.parametrize("with_searches", [False, True])
def test_legacy_state_migration_preserves_marketplace_and_backs_up(tmp_path, with_searches):
    store = Store(tmp_path / "legacy.sqlite3")
    watch = Watch(name="Saved watch", query="camera", countries=["FR"], exclude=["broken"])
    ident = store.add(watch, REGIONS)
    item = Listing("facebook", "1", "camera", "https://facebook.com/marketplace/item/1", "FR")
    store.record(store.due(1)[0], SearchResult([item], radius_km=500), 100)
    mixed = {**watch.model_dump(mode="json"), "sources": ["facebook", "ebay"]}
    retired = {**mixed, "sources": ["ebay"]}
    if with_searches:
        searches = Searches(store)
        search_id = searches.submit(SearchSpec(query="camera", countries=["FR"]))
        searches.record(searches.claim(), SearchResult([item]))
        photos = PhotoFilter(store, Settings(tmp_path))
        photos.save(ident, item, "key", "review", 0, "test")
        photos.save(
            ident, Listing(**{**asdict(item), "source": "ebay"}), "key", "review", 0, "test"
        )
        with store.connect() as db:
            db.execute("UPDATE searches SET spec=?", (json.dumps(mixed),))
            db.execute(
                "INSERT INTO searches(id,spec,created_at) VALUES ('retired',?,0)",
                (json.dumps(retired),),
            )
            db.execute(
                "INSERT INTO search_jobs(search_id,source,country,anchor,position) VALUES (?,'ebay','FR','country',2)",
                (search_id,),
            )
            db.execute(
                "INSERT INTO search_listings VALUES (?,'ebay','FR','2',?,1,0)",
                (search_id, json.dumps({**asdict(item), "source": "ebay"})),
            )
    with store.connect() as db:
        db.execute("DELETE FROM meta WHERE key=?", (MARKETPLACE_ONLY,))
        db.execute("UPDATE watches SET spec=? WHERE id=?", (json.dumps(mixed), ident))
        db.execute("INSERT INTO watches(spec) VALUES (?)", (json.dumps(retired),))
        db.execute(
            "INSERT INTO scans(watch_id,source,country,anchor) VALUES (?,'ebay','FR','country')",
            (ident,),
        )
        db.execute("INSERT INTO seen VALUES (?,'ebay','2',100)", (ident,))
        db.execute(
            "INSERT INTO outbox(watch_id,payload) VALUES (?,?)",
            (ident, json.dumps({"source": "ebay"})),
        )
        db.execute("INSERT INTO meta VALUES ('cooldown:ebay','9999999999')")
        db.execute("INSERT INTO meta VALUES ('cooldown:facebook','9999999999')")
        original_scans = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM scans WHERE source='facebook' ORDER BY watch_id,source,country,anchor"
            )
        ]
    migrated = Store(store.path)
    assert migrated.watches() == [{"id": ident, **watch.model_dump(mode="json")}]
    assert migrated.health()["scans"] == original_scans
    assert migrated.health()["pending_alerts"] == 1
    with migrated.connect() as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT count(*) FROM seen").fetchone()[0] == 1
        assert (
            db.execute("SELECT value FROM meta WHERE key='cooldown:facebook'").fetchone()[0]
            == "9999999999"
        )
        assert db.execute("SELECT 1 FROM meta WHERE key='cooldown:ebay'").fetchone() is None
    if with_searches:
        assert len(Searches(migrated).recent()) == 1
        assert searches.get(search_id)["spec"]["sources"] == ["facebook"]
        assert len(searches.get(search_id)["countries"][0]["regions"]) == 2
        assert searches.results(search_id, "FR")["total"] == 1
        with migrated.connect() as db:
            assert db.execute("SELECT count(*) FROM visual_checks").fetchone()[0] == 1
    backups = list(tmp_path.glob("*.before-marketplace-only-*.sqlite3"))
    assert len(backups) == 1
    assert backups[0].stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("SELECT count(*) FROM watches").fetchone()[0] == 2
        assert backup.execute("SELECT count(*) FROM scans WHERE source='ebay'").fetchone()[0] == 1
    Store(store.path)
    assert list(tmp_path.glob("*.before-marketplace-only-*.sqlite3")) == backups


def test_failed_backup_leaves_legacy_state_untouched(tmp_path, monkeypatch):
    from scout import migrations

    store = Store(tmp_path / "legacy.sqlite3")
    with store.connect() as db:
        db.execute("DELETE FROM meta WHERE key=?", (MARKETPLACE_ONLY,))
        db.execute("INSERT INTO watches(spec) VALUES (?)", (json.dumps({"sources": ["ebay"]}),))

    def fail(**kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(migrations.tempfile, "mkstemp", fail)
    with pytest.raises(OSError, match="disk full"):
        Store(store.path)
    with store.connect() as db:
        assert db.execute("SELECT spec FROM watches").fetchone()[0] == '{"sources": ["ebay"]}'
        assert db.execute("SELECT 1 FROM meta WHERE key=?", (MARKETPLACE_ONLY,)).fetchone() is None


def test_api_rejects_retired_source(tmp_path):
    store = Store(tmp_path / "api.sqlite3")
    settings = Settings(tmp_path, api_token="a" * 32)
    headers = {"Authorization": "Bearer " + settings.api_token}
    with TestClient(create_app(store, settings, start_worker=False)) as client:
        for sources in (["ebay"], ["facebook", "ebay"]):
            payload = {"name": "camera", "query": "camera", "countries": ["FR"], "sources": sources}
            for endpoint in ("watches", "searches"):
                assert (
                    client.post(f"/api/{endpoint}", json=payload, headers=headers).status_code
                    == 422
                )
        assert (
            client.post("/api/watches/1/review/ebay/1/notify", headers=headers).status_code == 422
        )
        assert "ebay_configured" not in client.get("/api/health", headers=headers).json()
        assert "ebay" not in client.get("/").text.lower()
        assert "ebay" not in client.get("/app.js").text.lower()
    assert image_key("https://i.ebayimg.com/image.jpg") is None
    assert image_key("https://scontent.fbcdn.net/image.jpg") is not None
