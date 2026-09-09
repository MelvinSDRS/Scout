import asyncio
import json
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from scout.api import create_app
from scout.models import Listing, SearchResult, Watch, matches
from scout.notify import deliver
from scout.providers.facebook import REGIONS, extract_listings, search_url
from scout.settings import Settings
from scout.store import Store
from scout.worker import worker_lock


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "test.sqlite3")


def watch(**kwargs):
    return Watch(name="Oakley", query="Oakley Judge", countries=["CA"], **kwargs)


def listing(ident="123", **kwargs):
    return Listing(
        "facebook",
        ident,
        "Oakley Judge watch",
        f"https://www.facebook.com/marketplace/item/{ident}/",
        "CA",
        **kwargs,
    )


def test_matching_terms_prices_and_exclusions():
    assert matches(watch(), listing())
    assert not matches(watch(exclude=["watch"]), listing())
    assert not matches(watch(include=["Oakley Judge II"]), listing())
    assert matches(
        watch(max_prices={"CAD": Decimal("200")}), listing(price="199.99", currency="CAD")
    )
    assert not matches(watch(max_prices={"CAD": 200}), listing(price="199", currency="USD"))
    assert not matches(watch(max_prices={"CAD": 200}), listing())
    assert not matches(watch(max_prices={"CAD": 200}), listing(price="NaN", currency="CAD"))
    assert not matches(watch(max_prices={"CAD": 200}), listing(price="-1", currency="CAD"))


@pytest.mark.parametrize(
    "payload",
    [
        {"countries": []},
        {"countries": ["GB"]},
        {"query": "  "},
        {"interval_minutes": 1},
        {"sources": []},
        {"max_prices": {"CAD": -1}},
        {"include": [" "]},
    ],
)
def test_validation(payload):
    with pytest.raises(ValidationError):
        Watch.model_validate({"name": "watch", "query": "camera", "countries": ["CA"], **payload})


def test_country_definitions_and_encoded_query():
    assert {country: len(anchors) for country, anchors in REGIONS.items()} == {
        "US": 13,
        "CA": 9,
        "FR": 2,
    }
    url = search_url("watch & camera", "FR", REGIONS["FR"][0]["city"])
    assert "query=watch+%26+camera" in url
    assert "radius=500" in url
    # Keep relevance ordering: newest-first hid known model matches in live searches.
    from urllib.parse import parse_qs, urlparse

    params = parse_qs(urlparse(url).query)
    assert "sortBy" not in params
    assert params["exact"] == ["true"]


def test_cross_region_dedup_and_restart(store):
    store.add(watch(), REGIONS)
    scans = store.due(1)
    store.record(scans[0], SearchResult([listing()]), 100)
    restarted = Store(store.path)
    restarted.record(scans[1], SearchResult([listing()]), 101)
    assert restarted.health()["pending_alerts"] == 1
    restarted.record(scans[0], SearchResult([listing(), listing("456")]), 5000)
    with restarted.connect() as db:
        rows = db.execute("SELECT payload FROM outbox ORDER BY id").fetchall()
    assert len(rows) == 2
    assert json.loads(rows[0][0])["initial"] is True
    assert json.loads(rows[1][0])["initial"] is False


def test_initial_ten_global_and_baseline_all(store):
    store.add(watch(), REGIONS)
    scans = store.due(1)
    store.record(scans[0], SearchResult([listing(str(i)) for i in range(15)]), 100)
    store.record(scans[1], SearchResult([listing(str(i)) for i in range(10, 25)]), 101)
    assert store.health()["pending_alerts"] == 10
    store.record(scans[0], SearchResult([listing(str(i)) for i in range(25)]), 5000)
    assert store.health()["pending_alerts"] == 10
    store.record(scans[0], SearchResult([listing("26")]), 10000)
    assert store.health()["pending_alerts"] == 11


def test_failure_does_not_baseline_or_advance_success(store):
    store.add(watch(), REGIONS)
    scan = store.due(1)[0]
    store.failed(scan, "Login required", 100)
    record = store.health()["scans"][0]
    assert not record["initialized"]
    assert record["last_success"] is None
    assert record["failures"] == 1
    assert record["next_run"] == 1900


def test_deleted_inflight_watch_cannot_queue(store):
    ident = store.add(watch(), REGIONS)
    scan = store.due(1)[0]
    store.remove(ident)
    store.record(scan, SearchResult([listing()]), 100)
    assert store.health()["pending_alerts"] == 0
    assert store.add(watch(), REGIONS) != ident


def test_capacity_guard(store):
    all_countries = Watch(
        name="wide", query="camera", countries=["US", "CA", "FR"], interval_minutes=30
    )
    store.add(all_countries, REGIONS)
    store.add(all_countries, REGIONS)
    with pytest.raises(ValueError, match="Capacity"):
        store.add(all_countries, REGIONS)


def test_outbox_retry_and_success(store, tmp_path, monkeypatch):
    settings = Settings(tmp_path, telegram_token="secret", telegram_chat="123")
    store.add(watch(), REGIONS)
    store.record(store.due(1)[0], SearchResult([listing()]), 100)
    responses = [httpx.Response(500), httpx.Response(200, json={"ok": True})]
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return responses.pop(0)

    async def no_sleep(_):
        pass

    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await deliver(store, settings, client)
            assert store.health()["pending_alerts"] == 1
            assert store.health()["failed_alerts"] == 1
            with store.connect() as db:
                db.execute("UPDATE outbox SET next_try=0")
            await deliver(store, settings, client)
            await deliver(store, settings, client)

    asyncio.run(exercise())
    assert len(seen) == 2
    assert seen[0]["chat_id"] == "123"
    assert store.health()["sent_alerts"] == 1


def test_api_auth_origin_and_crud(store, tmp_path):
    settings = Settings(tmp_path, api_token="a" * 32)
    with TestClient(create_app(store, settings, start_worker=False)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/watches").status_code == 401
        headers = {"Authorization": "Bearer " + settings.api_token}
        assert (
            client.post(
                "/api/watches",
                json=watch().model_dump(mode="json"),
                headers={**headers, "Origin": "https://evil.test"},
            ).status_code
            == 403
        )
        result = client.post("/api/watches", json=watch().model_dump(mode="json"), headers=headers)
        assert result.status_code == 201
        ident = result.json()["id"]
        assert client.get("/api/watches", headers=headers).json()[0]["id"] == ident
        assert len(client.get("/api/health", headers=headers).json()["scans"]) == 9
        assert client.delete(f"/api/watches/{ident}", headers=headers).status_code == 200
        assert client.delete(f"/api/watches/{ident}", headers=headers).status_code == 404
        assert client.get("/", headers={"Host": "evil.test"}).status_code == 400


def test_worker_exclusivity(tmp_path):
    with worker_lock(tmp_path):
        with pytest.raises(RuntimeError, match="Another worker"):
            with worker_lock(tmp_path):
                pass


def test_facebook_structured_data():
    obj = {
        "data": {
            "edges": [
                {
                    "node": {
                        "listing": {
                            "id": "1234",
                            "marketplace_listing_title": "Oakley Judge",
                            "listing_price": {"amount": "150.00", "currency": "CAD"},
                        }
                    }
                }
            ]
        }
    }
    result = extract_listings([obj, obj, {"id": "5", "title": "unrelated"}], "CA")
    assert len(result) == 1
    assert result[0].price == "150.00"
    assert result[0].currency == "CAD"


def test_login_resumes_only_facebook_preserving_dedup(store):
    store.add(
        Watch(name="test", query="Oakley Judge", countries=["CA"], sources=["facebook"]),
        REGIONS,
    )
    scan = next(s for s in store.due(1) if s["source"] == "facebook")
    store.record(scan, SearchResult([listing()], radius_km=500, coverage_warning="test cap"), 100)
    with store.connect() as db:
        db.execute("INSERT INTO meta VALUES ('cooldown:facebook','9999999999')")
    store.resume_source("facebook")
    with store.connect() as db:
        assert db.execute("SELECT 1 FROM meta WHERE key='cooldown:facebook'").fetchone() is None
        saved = db.execute(
            "SELECT * FROM scans WHERE watch_id=? AND source=? AND country=? AND anchor=?",
            store.key(scan),
        ).fetchone()
        assert saved["next_run"] == 0
        assert saved["initialized"] == 1
        assert saved["radius_km"] == 500
    store.record(scan, SearchResult([listing()]), 200)
    assert store.health()["pending_alerts"] == 1


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Oakley Judge", True),
        ("Montre Oakley Judge", True),
        ("Oakley Judge watch", True),
        ("Oakley Judge sunglasses", False),
        ("Lunettes Oakley Judge", False),
        ("Oakley sunglasses", False),
    ],
)
def test_watch_model_matching_excludes_eyewear(title, expected):
    spec = Watch(
        name="Oakley Judge",
        query="Oakley Judge",
        countries=["CA", "US", "FR"],
        exclude=["glasses", "sunglasses", "eyeglasses", "eyewear", "lunettes"],
    )
    item = Listing("facebook", "1", title, "https://www.facebook.com/marketplace/item/1/", "CA")
    assert matches(spec, item) is expected


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Bioworld Marvel Spider-Man Backpack", True),
        ("Spiderman backpack", True),
        ("Spider Man back pack", True),
        ("Sac à dos Spiderman", True),
        ("Spider-Man bookbag", True),
        ("Spider-Man mini backpack", True),
        ("Spider-Man Loungefly Backpack", False),
        ("Spider-Man backpack keychain", False),
        ("Spider-Man action figure", False),
        ("Batman backpack", False),
    ],
)
def test_alternative_title_groups(title, expected):
    w = Watch(
        name="backpack",
        query="spider-man backpack",
        countries=["CA"],
        include_any=[
            ["spider-man", "spiderman"],
            ["backpack", "back pack", "bookbag", "sac à dos"],
        ],
        exclude=["loungefly", "keychain"],
    )
    item = Listing("facebook", "1", title, "https://facebook.com/marketplace/item/1/", "CA")
    assert matches(w, item) is expected


def test_invalid_alternative_groups():
    for groups in [[[]], [[" "]], [["ok"] * 21]]:
        with pytest.raises(ValidationError):
            Watch(name="test", query="test", countries=["CA"], include_any=groups)
