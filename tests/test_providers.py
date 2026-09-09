import asyncio

import pytest

from scout.models import AccessBlocked
from scout.settings import Settings


def test_facebook_access_block_pauses_remaining_regions(tmp_path, monkeypatch):
    from scout import worker
    from scout.models import Watch
    from scout.providers.facebook import REGIONS
    from scout.store import Store

    calls = []

    class BlockedFacebook:
        def __init__(self, settings):
            pass

        async def search(self, query, country, anchor):
            calls.append(("facebook", country))
            raise AccessBlocked("Login required")

        async def close(self):
            pass

    async def no_delivery(*args):
        pass

    async def no_sleep(*args):
        pass

    monkeypatch.setattr(worker, "Facebook", BlockedFacebook)
    monkeypatch.setattr(worker, "deliver", no_delivery)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    store = Store(tmp_path / "worker.sqlite3")
    store.add(
        Watch(name="test", query="Oakley Judge", countries=["CA", "FR"], sources=["facebook"]),
        REGIONS,
    )
    asyncio.run(worker.run(store, Settings(tmp_path), once=True))
    assert sum(source == "facebook" for source, _ in calls) == 1
    assert store.health()["pending_alerts"] == 0
    assert sum(scan["initialized"] for scan in store.health()["scans"]) == 0


def test_facebook_explicit_empty_and_scoped_data():
    from scout.providers.facebook_data import confirmed_empty, extract_listings

    empty = {
        "marketplace_search": {
            "feed_units": {
                "edges": [
                    {
                        "node": {
                            "__typename": "MarketplaceSearchFeedNoResults",
                            "is_main_results_empty": True,
                            "is_relaxation_results_empty": True,
                        }
                    }
                ]
            }
        }
    }
    assert confirmed_empty([empty])
    assert not confirmed_empty([{"edges": []}])
    outside = {"id": "999", "marketplace_listing_title": "Oakley Judge in Messenger"}
    assert extract_listings([empty, outside], "CA", scoped=True) == []


def test_facebook_rendered_currency_and_sold_filter():
    from scout.providers.facebook_data import extract_listings, price_currency

    assert price_currency({"formatted_amount": "100 $CA"}) == "CAD"
    assert price_currency({"formatted_amount": "100 US$"}) == "USD"
    assert price_currency({"formatted_amount": "100 €"}) == "EUR"
    assert price_currency({"formatted_amount": "$100"}) is None
    assert price_currency({"formatted_amount": "$100"}, "CAD") == "CAD"
    base = {
        "marketplace_listing_title": "Oakley Judge",
        "listing_price": {"amount": "100", "formatted_amount": "100 $CA"},
        "location": {"reverse_geocode": {"city": "Montreal", "state": "QC"}},
    }
    rows = extract_listings(
        [
            {**base, "id": "1"},
            {**base, "id": "2", "is_sold": True},
            {**base, "id": "3", "is_pending": True},
        ],
        "US",
    )
    assert len(rows) == 1
    assert (
        rows[0].currency == "CAD"
    )  # Currency follows the displayed amount, never the search country.
    assert rows[0].location == "Montreal, QC"


def test_facebook_radius_and_pagination_metadata():
    from scout.providers.facebook_data import applied_radius, more_results, radius_option

    assert applied_radius([{"params": {"filter_radius_km": 65}}, {"filter_radius_km": 500}]) == 500
    assert radius_option("500 kilomètres") == 500
    assert radius_option("500 miles") == 805
    assert radius_option("unknown") is None
    assert more_results(
        [{"marketplace_search": {"feed_units": {"page_info": {"has_next_page": True}}}}]
    )


@pytest.mark.parametrize(
    "options,desired,expected",
    [
        (["250 kilomètres", "500 kilomètres"], 500, 500),
        (["250 kilomètres", "500 kilomètres"], 805, 500),
        (["250 miles", "500 miles"], 500, 805),
        (["250 miles", "500 miles"], 805, 805),
    ],
)
def test_radius_unit_switch_does_not_unnecessarily_shrink_coverage(options, desired, expected):
    from scout.providers.facebook_data import choose_radius

    assert choose_radius(options, desired)[0] == expected
