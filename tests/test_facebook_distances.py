import asyncio
import json
from dataclasses import replace

import pytest

from scout.models import AccessBlocked, Listing, SearchSpec
from scout.providers.facebook import Facebook
from scout.providers.facebook_data import applied_search_center, listing_coordinates
from scout.settings import Settings

CENTER = (45.5038, -73.5744)
OTTAWA = (45.4215, -75.6972)


def item(ident="1", title="camera"):
    return Listing(
        "facebook",
        ident,
        title,
        f"https://facebook.com/marketplace/item/{ident}/",
        "CA",
        "100",
        "CAD",
    )


def spec():
    return SearchSpec(
        query="camera",
        countries=["CA"],
        pricing={"city": "montreal", "label": "Montreal", "radius_km": 20},
    )


def test_search_center_uses_verified_radius_and_rejects_conflicts():
    def params(radius, lat, lon):
        return {
            "filter_radius_km": radius,
            "filter_location_latitude": lat,
            "filter_location_longitude": lon,
        }

    docs = [params(500, *OTTAWA), params(20, *CENTER)]
    assert applied_search_center(docs, 20) == CENTER
    assert applied_search_center([*docs, params(20, *OTTAWA)], 20) is None
    assert applied_search_center([{"latitude": CENTER[0], "longitude": CENTER[1]}], 20) is None


def test_listing_coordinate_attribution_ignores_buyer_route_and_other_items():
    docs = [
        {
            "id": "1",
            "isCrawler": False,
            "location": {"latitude": CENTER[0], "longitude": CENTER[1]},
        },
        {
            "id": "2",
            "marketplace_listing_title": "camera",
            "location": {"latitude": CENTER[0], "longitude": CENTER[1]},
        },
        {
            "id": "1",
            "marketplace_listing_title": "camera",
            "location": {"latitude": OTTAWA[0], "longitude": OTTAWA[1]},
        },
    ]
    assert listing_coordinates(docs, "1") == OTTAWA
    assert listing_coordinates(docs[:2], "1") is None
    assert (
        listing_coordinates(
            [
                *docs,
                {
                    "id": "1",
                    "marketplace_listing_title": "camera",
                    "location": {"latitude": CENTER[0], "longitude": CENTER[1]},
                },
            ],
            "1",
        )
        is None
    )


def test_enrichment_only_fetches_matching_unmeasured_listings(tmp_path, monkeypatch):
    provider = Facebook(Settings(tmp_path))
    calls = []

    async def coordinates(listing):
        calls.append(listing.id)
        return OTTAWA if listing.id == "2" else CENTER

    monkeypatch.setattr(provider, "_detail_coordinates", coordinates)
    listings = [
        item(),
        item("2"),
        item("3", "unrelated"),
        replace(item("4"), distance_km=1),
        replace(item("5"), status="pending"),
    ]
    result = asyncio.run(provider._measure_pricing_listings(listings, CENTER, spec()))
    assert calls == ["1", "2"]
    assert result[0].distance_km == 0
    assert result[1].distance_km > 160
    assert result[2].distance_km is None
    assert result[3].distance_km == 1
    assert result[4].distance_km is None


def test_detail_lookup_budget_keeps_unchecked_locations_unknown(tmp_path, monkeypatch):
    provider = Facebook(Settings(tmp_path))
    calls = []

    async def coordinates(listing):
        calls.append(listing.id)
        return CENTER

    monkeypatch.setattr(provider, "_detail_coordinates", coordinates)
    result = asyncio.run(
        provider._measure_pricing_listings([item(str(n)) for n in range(30)], CENTER, spec())
    )
    assert len(calls) == 24
    assert sum(row.distance_km is None for row in result) == 6


@pytest.mark.parametrize("error", [AccessBlocked("Login required"), asyncio.CancelledError()])
def test_detail_enrichment_propagates_access_block_and_cancellation(tmp_path, monkeypatch, error):
    provider = Facebook(Settings(tmp_path))

    async def fail(listing):
        raise error

    monkeypatch.setattr(provider, "_detail_coordinates", fail)
    with pytest.raises(type(error)):
        asyncio.run(provider._measure_pricing_listings([item()], CENTER, spec()))


def test_detail_timeout_never_assumes_local(tmp_path, monkeypatch):
    provider = Facebook(Settings(tmp_path))

    async def fail(listing):
        raise TimeoutError()

    monkeypatch.setattr(provider, "_detail_coordinates", fail)
    result = asyncio.run(provider._measure_pricing_listings([item()], CENTER, spec()))
    assert result[0].distance_km is None


def test_detail_page_closes_and_attributes_coordinates(tmp_path, monkeypatch):
    class Page:
        closed = False

        async def goto(self, url, **kwargs):
            assert url == "https://www.facebook.com/marketplace/item/1/"
            return None

        def locator(self, selector):
            return self

        async def all_text_contents(self):
            return [
                json.dumps(
                    {
                        "id": "1",
                        "marketplace_listing_title": "camera",
                        "location": {"latitude": OTTAWA[0], "longitude": OTTAWA[1]},
                    }
                )
            ]

        async def close(self):
            self.closed = True

    page = Page()

    class Context:
        async def new_page(self):
            return page

    async def check_access(page):
        pass

    provider = Facebook(Settings(tmp_path))
    provider.context = Context()
    monkeypatch.setattr("scout.providers.facebook.check_access", check_access)
    assert asyncio.run(provider._detail_coordinates(item())) == OTTAWA
    assert page.closed
