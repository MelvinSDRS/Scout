import asyncio
import time

import pytest
from PIL import Image

from scout.image_filter import PhotoFilter, image_key
from scout.models import Listing, SearchResult, Watch
from scout.providers.facebook import REGIONS
from scout.settings import Settings
from scout.store import Store


@pytest.fixture
def setup(tmp_path):
    references = tmp_path / "image-references" / "example-bag"
    references.mkdir(parents=True)
    Image.new("RGB", (16, 16), "red").save(references / "front.jpg")
    store = Store(tmp_path / "test.db")
    spec = Watch(
        name="Photo watch",
        query="spiderman backpack",
        countries=["FR"],
        image_profile="example-bag",
    )
    ident = store.add(spec, REGIONS)
    return store, PhotoFilter(store, Settings(tmp_path)), spec, store.due(1)[0], ident


def listing(ident="1", photo="https://scontent.example.fbcdn.net/photo.jpg?token=one"):
    return Listing(
        "facebook",
        ident,
        "Spiderman backpack",
        f"https://facebook.com/marketplace/item/{ident}/",
        "FR",
        image_url=photo,
    )


def test_image_host_allowlist_and_rotating_signatures():
    assert image_key("https://a.fbcdn.net/a.jpg?one") == image_key("https://a.fbcdn.net/a.jpg?two")
    assert image_key("https://a.fbcdn.net/a.jpg?one") == image_key("https://b.fbcdn.net/a.jpg?two")
    for url in [
        "http://a.fbcdn.net/a",
        "https://evil.test/a",
        "https://fbcdn.net.evil.test/a",
        "file:///etc/passwd",
        None,
    ]:
        assert image_key(url) is None


def test_match_review_cache_and_manual_approval(setup, monkeypatch):
    store, photos, spec, scan, ident = setup
    calls = []

    async def inspect(item, profile):
        calls.append(item.id)
        return {
            "decision": "match" if item.id == "1" else "review",
            "inliers": 19 if item.id == "1" else 3,
        }

    monkeypatch.setattr(photos, "inspect", inspect)
    result = SearchResult([listing("1"), listing("2")], radius_km=500)
    filtered = asyncio.run(photos.filter(scan, spec, result))
    assert [x.id for x in filtered.listings] == ["1"]
    assert filtered.collected_count == 2
    store.record(scan, filtered, time.time())
    assert store.health()["scans"][0]["result_count"] == 2
    assert photos.review(ident)["total"] == 1
    asyncio.run(photos.filter(scan, spec, result))
    assert calls == ["1", "2"]  # no repeat work for delivered or cached uncertain photos
    photos.approve(ident, "facebook", "2")
    photos.approve(ident, "facebook", "2")
    assert store.health()["pending_alerts"] == 2
    assert photos.review(ident)["total"] == 0


def test_photo_failure_and_missing_photos_are_quiet_review(setup, monkeypatch):
    store, photos, spec, scan, ident = setup

    async def fail(item, profile):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(photos, "inspect", fail)
    filtered = asyncio.run(photos.filter(scan, spec, SearchResult([listing(), listing("2", None)])))
    assert filtered.listings == []
    assert photos.review(ident)["total"] == 2
    assert store.health()["pending_alerts"] == 0


def test_work_is_bounded_per_scan_and_hour(setup, monkeypatch):
    _, photos, spec, scan, ident = setup
    calls = []

    async def inspect(item, profile):
        calls.append(item.id)
        return {"decision": "review", "inliers": 0}

    monkeypatch.setattr(photos, "inspect", inspect)
    asyncio.run(photos.filter(scan, spec, SearchResult([listing(str(n)) for n in range(12)])))
    assert len(calls) == 8
    assert photos.review(ident)["total"] == 12
    for _ in range(52):
        assert photos.reserve()
    assert not photos.reserve()


def test_title_rejections_never_download_and_other_watches_unchanged(setup, monkeypatch):
    _, photos, spec, scan, _ = setup

    async def fail(item, profile):
        pytest.fail("Must not download")

    monkeypatch.setattr(photos, "inspect", fail)
    excluded = spec.model_copy(update={"exclude": ["backpack"]})
    assert asyncio.run(photos.filter(scan, excluded, SearchResult([listing()]))).listings == []
    plain = spec.model_copy(update={"image_profile": None})
    assert asyncio.run(photos.filter(scan, plain, SearchResult([listing()]))).listings == [
        listing()
    ]


def test_verified_photo_not_lost_to_old_initial_cap(setup):
    store, _, _, scan, _ = setup
    with store.connect() as db:
        db.execute("INSERT INTO meta VALUES ('initial:1','10')")
    store.record(scan, SearchResult([listing()]), time.time())
    assert store.health()["pending_alerts"] == 1


def test_paused_watch_does_not_deliver(setup):
    import httpx

    from scout.notify import deliver

    store, _, spec, scan, ident = setup
    store.record(scan, SearchResult([listing()]), time.time())
    with store.connect() as db:
        db.execute(
            "UPDATE watches SET spec=? WHERE id=?",
            (spec.model_copy(update={"enabled": False}).model_dump_json(), ident),
        )

    async def exercise():
        def handler(request):
            pytest.fail("Paused watch must not send")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await deliver(store, Settings(store.path.parent), client)

    asyncio.run(exercise())
    assert store.health()["pending_alerts"] == 1


def test_review_api_requires_auth_and_approvals_are_idempotent(setup):
    from fastapi.testclient import TestClient

    from scout.api import create_app

    store, photos, _, _, ident = setup
    photos.save(ident, listing(), "key", "review", 0, "Uncertain")
    settings = Settings(store.path.parent, api_token="x" * 32)
    with TestClient(create_app(store, settings, start_worker=False)) as client:
        url = f"/api/watches/{ident}/review"
        assert client.get(url).status_code == 401
        client.headers["Authorization"] = "Bearer " + settings.api_token
        assert client.get(url).json()["total"] == 1
        assert client.post(url + "/facebook/1/notify").status_code == 200
        assert client.post(url + "/facebook/1/notify").status_code == 200
        assert store.health()["pending_alerts"] == 1
        assert client.post(url + "/facebook/missing/notify").status_code == 404
