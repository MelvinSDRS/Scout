import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from scout.api import create_app
from scout.models import Listing, SearchResult, SearchSpec, Watch
from scout.notify import send
from scout.providers.facebook import REGIONS
from scout.searches import Searches
from scout.settings import Settings
from scout.store import Store


@pytest.fixture
def searches(tmp_path):
    return Searches(Store(tmp_path / "test.sqlite3"))


def spec(countries=None):
    return SearchSpec(query="Oakley Judge", countries=countries or ["FR"], exclude=["glasses"])


def listing(ident="1", title="Oakley Judge watch", country="FR"):
    return Listing(
        "facebook",
        ident,
        title,
        f"https://www.facebook.com/marketplace/item/{ident}/",
        country,
        "150",
        "EUR",
    )


def test_search_round_robin_and_single_active(searches):
    ident = searches.submit(spec(["US", "CA", "FR"]))
    with pytest.raises(ValueError, match="already running"):
        searches.submit(spec())
    for country in ["US", "CA", "FR", "US"]:
        job = searches.claim()
        assert job["country"] == country
        searches.record(job, SearchResult([]))
    assert searches.get(ident)["finished_regions"] == 4
    assert searches.store.watches() == []
    assert searches.store.health()["pending_alerts"] == 0


def test_results_filters_dedup_and_pagination(searches):
    ident = searches.submit(spec())
    result = SearchResult(
        [listing(), listing("2", "Oakley Judge glasses"), listing("3", "Other watch")],
        radius_km=500,
    )
    searches.record(searches.claim(), result)
    searches.record(searches.claim(), result)
    assert searches.get(ident)["status"] == "complete"
    assert searches.get(ident)["countries"][0]["listings"] == 3
    assert searches.results(ident, "FR")["total"] == 1
    assert searches.results(ident, "FR", True, 1, 1)["items"][0]["id"] == "2"
    assert searches.results(ident, "FR", True)["total"] == 3


def test_cancellation_ignores_inflight_and_recovery(searches):
    ident = searches.submit(spec())
    searches.claim()
    searches.recover()
    job = searches.claim()
    assert searches.cancel(ident)
    searches.record(job, SearchResult([listing()]))
    assert searches.get(ident)["status"] == "cancelled"
    assert searches.results(ident, "FR")["total"] == 0
    assert searches.claim() is None
    assert not searches.cancel("missing")


def test_failures_are_explicit_and_source_block_drains_queue(searches):
    ident = searches.submit(spec())
    searches.record(searches.claim(), SearchResult([]))
    searches.fail(searches.claim(), "Login required", True)
    assert searches.get(ident)["status"] == "partial"
    assert searches.get(ident)["countries"][0]["failed"] == 1
    ident = searches.submit(spec(["US", "CA", "FR"]))
    searches.fail(searches.claim(), "Login required", True)
    assert searches.get(ident)["status"] == "failed"
    assert searches.claim() is None


def test_conversion_baseline_atomic_idempotent_and_recreate(searches):
    ident = searches.submit(spec())
    searches.record(searches.claim(), SearchResult([listing()], radius_km=500))
    searches.record(searches.claim(), SearchResult([listing()], radius_km=500))
    watch_id = searches.create_watch(ident, interval=180)
    assert searches.create_watch(ident) == watch_id
    assert len(searches.store.watches()) == 1
    with searches.store.connect() as db:
        assert db.execute("SELECT count(*) FROM seen").fetchone()[0] == 1
    scans = searches.store.due(time.time() + 20000)
    assert all(s["initialized"] and s["radius_km"] == 500 for s in scans)
    assert all(s["next_run"] - s["last_success"] == 180 * 60 for s in scans)
    searches.store.record(scans[0], SearchResult([listing(), listing("4")]), time.time())
    assert searches.store.health()["pending_alerts"] == 1  # only newly discovered ID
    searches.store.remove(watch_id)
    assert searches.get(ident)["watch_id"] is None
    assert searches.create_watch(ident) != watch_id


def test_conversion_capacity_rolls_back(searches):
    for _ in range(2):
        searches.store.add(
            Watch(name="full", query="test", countries=["US", "CA", "FR"], interval_minutes=30),
            REGIONS,
        )
    ident = searches.submit(spec(["US", "CA", "FR"]))
    with pytest.raises(ValueError, match="Capacity"):
        searches.create_watch(ident)
    assert searches.get(ident)["watch_id"] is None
    assert len(searches.store.watches()) == 2


def test_search_hourly_budget(searches):
    for _ in range(4):
        searches.cancel(searches.submit(spec(["US", "CA", "FR"])))
    with pytest.raises(ValueError, match="hourly"):
        searches.submit(spec(["US"]))


def test_search_api_and_auth(searches, tmp_path):
    settings = Settings(
        tmp_path, api_token="x" * 32, telegram_chat="-1001234567890", telegram_thread=42
    )
    with TestClient(create_app(searches.store, settings, start_worker=False)) as client:
        assert client.get("/app.js").status_code == 200
        assert client.get("/style.css").status_code == 200
        assert client.post("/api/searches", json=spec().model_dump(mode="json")).status_code == 401
        client.headers["Authorization"] = "Bearer " + settings.api_token
        assert (
            client.post(
                "/api/searches",
                json=spec().model_dump(mode="json"),
                headers={"Origin": "https://evil.test"},
            ).status_code
            == 403
        )
        assert (
            client.get("/api/health").json()["telegram_topic_url"] == "https://t.me/c/1234567890/42"
        )
        ident = client.post("/api/searches", json=spec().model_dump(mode="json")).json()["id"]
        assert client.post("/api/searches", json=spec().model_dump(mode="json")).status_code == 409
        assert client.get(f"/api/searches/{ident}").json()["status"] == "running"
        assert client.get(f"/api/searches/{ident}/results?country=FR&limit=101").status_code == 422
        assert client.get(f"/api/searches/{ident}/results?country=FR").json()["items"] == []
        assert client.get("/api/searches/missing").status_code == 404
        assert (
            client.post(f"/api/searches/{ident}/watch", json={"interval_minutes": 1}).status_code
            == 422
        )
        assert client.post(f"/api/searches/{ident}/watch", json={}).status_code == 201
        assert client.post(f"/api/searches/{ident}/cancel").status_code == 200


@pytest.mark.parametrize("thread", [42, None])
def test_notification_topic(tmp_path, thread):
    payloads = []

    def handle(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 2}})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await send(
                Settings(
                    tmp_path,
                    telegram_token="test",
                    telegram_chat="-1001234567890",
                    telegram_thread=thread,
                ),
                "test",
                client,
            )
            assert result["message_id"] == 2

    asyncio.run(exercise())
    assert payloads[0].get("message_thread_id") == thread
    if thread is None:
        assert "message_thread_id" not in payloads[0]


def test_worker_shares_provider_and_fairly_serves_watches(searches, tmp_path, monkeypatch):
    from scout import worker

    calls = []

    class Provider:
        def __init__(self, settings):
            pass

        async def search(self, query, country, anchor):
            calls.append(query)
            return SearchResult([])

        async def close(self):
            pass

    async def noop(*args):
        pass

    monkeypatch.setattr(worker, "Facebook", Provider)
    monkeypatch.setattr(worker, "deliver", noop)
    monkeypatch.setattr(asyncio, "sleep", noop)
    searches.store.add(Watch(name="scheduled", query="Scheduled watch", countries=["FR"]), REGIONS)
    ident = searches.submit(spec(["US"]))
    searches.claim()  # restart recovery
    asyncio.run(worker.run(searches.store, Settings(tmp_path), once=True))
    assert calls[:3] == ["Oakley Judge", "Oakley Judge", "Scheduled watch"]
    assert calls.count("Oakley Judge") == 13
    assert calls.count("Scheduled watch") == 2
    assert searches.get(ident)["status"] == "complete"


def test_history_retention_cannot_reset_hourly_budget(searches):
    for _ in range(50):
        searches.cancel(searches.submit(spec()))
    assert len(searches.recent()) == 20
    with pytest.raises(ValueError, match="hourly"):
        searches.submit(spec())


def test_public_hostname_preserves_auth_and_same_origin(searches, tmp_path):
    settings = Settings(
        tmp_path, api_token="x" * 32, allowed_hosts=("localhost", "scout.example.com")
    )
    with TestClient(
        create_app(searches.store, settings, start_worker=False),
        base_url="https://scout.example.com",
    ) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/watches").status_code == 401
        client.headers["Authorization"] = "Bearer " + settings.api_token
        client.headers["Origin"] = "https://scout.example.com"
        assert client.get("/api/watches").status_code == 200
        assert (
            client.get("/api/watches", headers={"Origin": "https://evil.example"}).status_code
            == 403
        )
        assert client.get("/", headers={"Host": "evil.example"}).status_code == 400
