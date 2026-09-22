import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from scout.api import create_app
from scout.models import Listing, SearchResult, SearchSpec, Watch
from scout.pricing import amount
from scout.searches import Searches
from scout.settings import Settings
from scout.store import Store


def spec(**changes):
    return SearchSpec.model_validate(
        {
            "query": "camera",
            "countries": ["CA"],
            "pricing": {"city": "montreal", "label": "Montréal", "radius_km": 40},
            **changes,
        }
    )


def listing(ident="1", price="100", **changes):
    return replace(
        Listing(
            "facebook",
            ident,
            "Camera",
            f"https://facebook.com/marketplace/item/{ident}/",
            "CA",
            price,
            "CAD",
            distance_km=5,
        ),
        **changes,
    )


@pytest.fixture
def searches(tmp_path):
    return Searches(Store(tmp_path / "test.sqlite3"))


def record(searches, items, **options):
    ident = searches.submit(spec(**options))
    searches.record(searches.claim(), SearchResult(items, radius_km=40))
    return ident


def test_one_city_statistics_and_robust_suggestion(searches):
    ident = record(
        searches,
        [listing(str(i), price) for i, price in enumerate(["100", "200", "300", "400", "9000"])],
    )
    status = searches.get(ident)
    assert status["total_regions"] == 1
    assert status["countries"][0]["regions"][0]["name"] == "Montréal"
    report = searches.pricing_report(ident)
    assert (report["minimum"], report["average"], report["maximum"]) == (
        "100.00",
        "2000.00",
        "9000.00",
    )
    assert report["suggested_price"] == "300.00"
    assert report["quick_sale_price"] == "200.00"
    assert report["patient_price"] == "400.00"
    assert sum(b["count"] for b in report["histogram"]) == 5
    with pytest.raises(ValueError, match="cannot be converted"):
        searches.create_watch(ident)
    assert searches.store.watches() == []
    assert searches.store.health()["pending_alerts"] == 0


def test_currency_invalid_prices_statuses_and_title_filters(searches):
    items = [
        listing("1"),
        listing("2", "500", currency="USD"),
        listing("3", "0"),
        listing("4", "NaN"),
        listing("5", "-1"),
        listing("6", None),
        listing("7", "10", status="pending"),
        listing("8", "90", status="sold"),
        listing("9", "1", title="Camera case"),
        listing("10", "100", title="Unrelated"),
        listing("11", "100", price_kind="monthly"),
    ]
    ident = record(searches, items, exclude=["case"])
    report = searches.pricing_report(ident)
    assert report["sample_size"] == 1
    assert report["excluded_count"] == 6
    assert report["minimum"] == "100.00"
    assert report["suggested_price"] is None
    assert report["sold_count"] == 1
    assert report["sold_items"][0]["observed_days"] is None
    assert report["speed_points"] == []


def test_radius_boundary_filters_current_pricing_and_new_history(searches):
    ident = record(
        searches,
        [
            listing("local", "100", distance_km=40),
            listing("outside", "900", distance_km=40.1),
            listing("missing", "800", distance_km=None),
            listing("boolean", "700", distance_km=True),
            listing("nan", "600", distance_km=float("nan")),
        ],
    )

    report = searches.pricing_report(ident)

    assert report["sample_size"] == 1
    assert report["minimum"] == "100.00"
    assert report["excluded_count"] == 0
    assert report["outside_radius_count"] == 1
    assert report["unknown_distance_count"] == 3
    assert any("outside the requested 40 km radius" in warning for warning in report["warnings"])
    assert any("rescan" in warning for warning in report["warnings"])
    with searches.store.connect() as db:
        assert db.execute("SELECT count(*) FROM price_observations").fetchone()[0] == 1


def test_legacy_current_payload_without_distance_has_no_estimate(searches):
    ident = record(searches, [listing()])
    with searches.store.connect() as db:
        payload = json.loads(
            db.execute(
                "SELECT payload FROM search_listings WHERE search_id=?", (ident,)
            ).fetchone()[0]
        )
        payload.pop("distance_km")
        db.execute(
            "UPDATE search_listings SET payload=? WHERE search_id=?",
            (json.dumps(payload), ident),
        )

    report = searches.pricing_report(ident)

    assert report["sample_size"] == 0
    assert report["suggested_price"] is None
    assert report["unknown_distance_count"] == 1
    assert any(
        "no verified distance" in warning and "rescan" in warning for warning in report["warnings"]
    )


def test_legacy_sold_history_without_distance_is_excluded_at_read(searches):
    record(searches, [listing(price="100")])
    ident = record(searches, [listing(price="90", status="sold")])
    with searches.store.connect() as db:
        row = db.execute("SELECT payload FROM price_observations").fetchone()
        payload = json.loads(row[0])
        payload.pop("distance_km")
        db.execute("UPDATE price_observations SET payload=?", (json.dumps(payload),))

    report = searches.pricing_report(ident)

    assert report["sold_count"] == 0
    assert report["speed_points"] == []
    assert any(
        "history without a verified local distance" in warning for warning in report["warnings"]
    )


def test_legacy_active_history_does_not_create_verified_sale_duration(searches):
    record(searches, [listing(price="100")])
    with searches.store.connect() as db:
        row = db.execute("SELECT payload FROM price_observations").fetchone()
        payload = json.loads(row[0])
        payload.pop("distance_km")
        db.execute("UPDATE price_observations SET payload=?", (json.dumps(payload),))

    ident = record(searches, [listing(price="90", status="sold")])
    report = searches.pricing_report(ident)

    assert report["sold_count"] == 1
    assert report["sold_items"][0]["observed_days"] is None
    assert report["speed_points"] == []


def test_outside_sold_history_is_excluded_at_read(searches):
    record(searches, [listing(price="100")])
    ident = record(searches, [listing(price="90", status="sold")])
    with searches.store.connect() as db:
        row = db.execute("SELECT payload FROM price_observations").fetchone()
        payload = json.loads(row[0])
        payload["distance_km"] = 41
        db.execute("UPDATE price_observations SET payload=?", (json.dumps(payload),))

    report = searches.pricing_report(ident)

    assert report["sold_count"] == 0
    assert any(
        "sold history outside the requested 40 km radius" in warning
        for warning in report["warnings"]
    )


@pytest.mark.parametrize("price", ["Infinity", "-Infinity", "sNaN", "abc", "1e10000", "1e-10000"])
def test_untrusted_prices(price):
    assert amount({"price": price, "currency": "CAD"}, "CAD") is None


def test_observed_lifecycle_missing_is_not_sold_and_relisting_resets(searches, monkeypatch):
    from scout import pricing
    from scout import searches as search_module

    now = [1_900_000_000.0]
    monkeypatch.setattr(search_module.time, "time", lambda: now[0])
    monkeypatch.setattr(pricing.time, "time", lambda: now[0])
    record(searches, [listing()])
    now[0] += 86400
    empty = record(searches, [])
    assert searches.pricing_report(empty)["sold_count"] == 0
    record(searches, [listing(price="80")])
    now[0] += 86400
    sold = record(searches, [listing(price="70", status="sold")])
    report = searches.pricing_report(sold)
    assert report["speed_points"] == [{"id": "1", "title": "Camera", "price": "80.00", "days": 2.0}]
    assert report["sold_items"][0]["price"] == "70"
    now[0] += 86400
    absent = record(searches, [])
    assert searches.pricing_report(absent)["sold_count"] == 1
    another_city = record(
        searches, [], pricing={"city": "toronto", "label": "Toronto", "radius_km": 40}
    )
    assert searches.pricing_report(another_city)["sold_count"] == 0
    relisted = record(searches, [listing()])
    assert searches.pricing_report(relisted)["sold_count"] == 0
    now[0] += 86400
    resold = record(searches, [listing(status="sold")])
    assert searches.pricing_report(resold)["speed_points"][0]["days"] == 1
    now[0] += 91 * 86400
    latest = record(searches, [])
    assert searches.pricing_report(latest)["sold_count"] == 0


def test_cancelled_scan_does_not_record_history(searches):
    ident = searches.submit(spec())
    job = searches.claim()
    searches.cancel(ident)
    searches.record(job, SearchResult([listing(status="sold")]))
    assert searches.pricing_report(ident)["sold_count"] == 0
    with searches.store.connect() as db:
        assert db.execute("SELECT count(*) FROM price_observations").fetchone()[0] == 0


def test_sold_history_preserves_currency_of_last_active_price(searches):
    record(searches, [listing(price="100", currency="CAD")])
    ident = record(searches, [listing(price="75", currency="USD", status="sold")])
    sold = searches.pricing_report(ident)["sold_items"][0]
    assert sold["last_active_price"] == "100.00"
    assert sold["last_active_currency"] == "CAD"
    assert sold["price"] == "75"
    assert sold["currency"] == "USD"


@pytest.mark.parametrize(
    "changes",
    [
        {"countries": ["US", "CA"]},
        {"max_prices": {"CAD": Decimal(100)}},
        {"image_profile": "anything"},
        {"pricing": {"city": "../evil", "label": "x", "radius_km": 40}},
        {"pricing": {"city": "montreal", "label": "x", "radius_km": 0}},
        {"pricing": {"city": "montreal", "label": "x", "radius_km": 806}},
    ],
)
def test_pricing_validation(changes):
    with pytest.raises(ValidationError):
        spec(**changes)


def test_regular_search_and_watch_stay_country_wide(searches):
    plain = SearchSpec(query="camera", countries=["CA"])
    ident = searches.submit(plain)
    assert searches.get(ident)["total_regions"] == 9
    with pytest.raises(ValueError, match="not a price check"):
        searches.pricing_report(ident)
    with pytest.raises(ValidationError):
        Watch(**spec().model_dump(), name="local")


def test_price_api_auth_errors_and_hourly_budget(searches, tmp_path):
    settings = Settings(tmp_path, api_token="x" * 32)
    with TestClient(create_app(searches.store, settings, start_worker=False)) as client:
        assert client.get("/api/searches/any/pricing").status_code == 401
        client.headers["Authorization"] = "Bearer " + settings.api_token
        assert client.get("/api/searches/missing/pricing").status_code == 404
        ident = client.post("/api/searches", json=spec().model_dump(mode="json")).json()["id"]
        assert client.get(f"/api/searches/{ident}/pricing").json()["sample_size"] == 0
        assert client.post(f"/api/searches/{ident}/watch", json={}).status_code == 422
        client.post(f"/api/searches/{ident}/cancel")
    for _ in range(99):
        searches.cancel(searches.submit(spec()))
    with pytest.raises(ValueError, match="hourly"):
        searches.submit(spec())


def test_worker_passes_local_scan_options(searches, tmp_path, monkeypatch):
    from scout import worker

    calls = []

    class Provider:
        def __init__(self, settings):
            pass

        @asynccontextmanager
        async def scan_session(self):
            yield self

        async def search(self, query, country, anchor, **options):
            calls.append((query, country, anchor, options))
            return SearchResult([listing()], radius_km=40)

        async def close(self):
            pass

    async def noop(*args):
        pass

    monkeypatch.setattr(worker, "Facebook", Provider)
    monkeypatch.setattr(worker, "deliver", noop)
    monkeypatch.setattr(asyncio, "sleep", noop)
    ident = searches.submit(spec())
    asyncio.run(
        worker.run(searches.store, Settings(tmp_path, facebook_restore_home=False), once=True)
    )
    assert calls[0][:3] == ("camera", "CA", "montreal")
    assert calls[0][3]["radius_km"] == 40
    assert calls[0][3]["include_sold"] is True
    assert calls[0][3]["pricing_spec"] == spec()
    assert searches.pricing_report(ident)["sample_size"] == 1
    assert searches.get(ident)["status"] == "complete"
