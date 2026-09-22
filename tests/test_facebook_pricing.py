import asyncio
import json
from urllib.parse import parse_qs, urlparse

import pytest

from scout.providers.facebook import REGIONS, Facebook, _verify_custom_anchor, search_url
from scout.providers.facebook_data import confirmed_empty, extract_listings
from scout.settings import Settings


def _feed(*rows):
    return {
        "marketplace_search": {
            "feed_units": {
                "edges": [{"node": {"listing": row}} for row in rows],
                "page_info": {"has_next_page": False},
            }
        }
    }


def _listing(ident, **flags):
    return {
        "id": str(ident),
        "marketplace_listing_title": "Oakley Judge",
        "listing_price": {"amount": "100", "currency": "CAD"},
        **flags,
    }


class _FakeLocator:
    def __init__(self, scripts=()):
        self.scripts = scripts
        self.first = self

    async def all_text_contents(self):
        return self.scripts

    async def count(self):
        return 0

    async def inner_text(self):
        return ""

    async def hover(self, **kwargs):
        return None


class _FakePage:
    url = "about:blank"

    def __init__(self, document):
        self.navigations = []
        self.script_locator = _FakeLocator([json.dumps(document)])

    def on(self, event, handler):
        return None

    async def goto(self, url, **kwargs):
        self.url = url
        self.navigations.append(url)
        return type("Response", (), {"status": 200})()

    def locator(self, selector):
        return self.script_locator

    def get_by_role(self, role):
        return _FakeLocator()

    async def close(self):
        return None


class _FakeContext:
    def __init__(self, page):
        self.page = page

    async def new_page(self):
        return self.page


def _custom_search_document(*rows, city_id="123456", city_name="Toronto"):
    return {
        "params": {
            "filter_radius_km": 25,
            "filter_location_latitude": 43.65,
            "filter_location_longitude": -79.38,
        },
        "viewer": {
            "marketplace_feed_stories": {"buy_location": {"id": city_id, "display_name": city_name}}
        },
        **_feed(*rows),
    }


def _run_fake_custom_search(tmp_path, monkeypatch, document, anchor="toronto"):
    async def no_access_check(page):
        return None

    page = _FakePage(document)
    provider = Facebook(Settings(tmp_path))
    provider.context = _FakeContext(page)
    monkeypatch.setattr("scout.providers.facebook.check_access", no_access_check)

    async def exercise():
        return await provider._search("Oakley Judge", "CA", anchor, radius_km=25, include_sold=True)

    return asyncio.run(exercise()), page


def test_custom_anchor_and_local_radius_are_encoded_without_region_lookup():
    url = search_url("Oakley Judge", "CA", "toronto-north-york", radius_km=25)

    parsed = urlparse(url)
    assert parsed.path == "/marketplace/toronto-north-york/search"
    assert parse_qs(parsed.query)["radius"] == ["25"]

    with pytest.raises(ValueError, match="anchor"):
        search_url("camera", "CA", "../toronto", radius_km=25)
    with pytest.raises(ValueError, match="between"):
        search_url("camera", "CA", "toronto", radius_km=0)


def test_include_sold_classifies_status_and_keeps_sold_only_feeds_structured():
    documents = [
        _feed(
            _listing("1"),
            _listing("2", is_sold=True, is_live=False),
            _listing("3", is_pending=True, is_live=False),
            _listing("4", is_hidden=True),
            _listing("5", is_live=False),
        )
    ]

    assert [item.id for item in extract_listings(documents, "CA", scoped=True)] == ["1"]
    rows = extract_listings(documents, "CA", scoped=True, include_sold=True)
    assert {item.id: item.status for item in rows} == {
        "1": "active",
        "2": "sold",
        "3": "pending",
    }

    sold_only = [_feed(_listing("6", is_sold=True, is_live=False))]
    assert not confirmed_empty(sold_only)
    assert [
        item.status for item in extract_listings(sold_only, "CA", scoped=True, include_sold=True)
    ] == ["sold"]


def test_scan_session_forwards_local_scan_options_without_bypassing_serialization(tmp_path):
    provider = Facebook(Settings(tmp_path))
    calls = []

    async def fake_search(query, country, anchor, *, radius_km=None, include_sold=False):
        calls.append((query, country, anchor, radius_km, include_sold))
        return "result"

    provider._search = fake_search
    session = provider.scan_session()
    session._entered = True

    async def exercise():
        return await session.search(
            "Oakley Judge",
            "CA",
            "toronto",
            radius_km=25,
            include_sold=True,
        )

    assert asyncio.run(exercise()) == "result"
    assert calls == [("Oakley Judge", "CA", "toronto", 25, True)]


def test_search_uses_custom_url_radius_and_accepts_sold_only_structured_feed(tmp_path, monkeypatch):
    sold = _listing("9", is_sold=True, is_live=False)
    result, page = _run_fake_custom_search(tmp_path, monkeypatch, _custom_search_document(sold))
    assert page.navigations == [search_url("Oakley Judge", "CA", "toronto", radius_km=25)]
    assert result.radius_km == 25
    assert "Marketplace city: Toronto" in result.coverage_warning
    assert [(item.id, item.status) for item in result.listings] == [("9", "sold")]


def test_search_rejects_custom_city_that_falls_back_to_another_city(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match="search city"):
        _run_fake_custom_search(
            tmp_path,
            monkeypatch,
            _custom_search_document(
                _listing("10", is_sold=True, is_live=False), city_name="Montreal"
            ),
        )


def test_existing_region_radius_url_and_call_shape_remain_unchanged():
    city = REGIONS["CA"][0]["city"]
    url = search_url("camera", "CA", city)
    assert parse_qs(urlparse(url).query)["radius"] == [str(REGIONS["CA"][0]["radius"])]


@pytest.mark.parametrize(
    "documents,anchor",
    [
        ([], "toronto"),
        ([_custom_search_document()], "999999"),
        ([_custom_search_document(), _custom_search_document(city_id="456789")], "toronto"),
    ],
)
def test_city_identity_missing_mismatch_or_conflicting_fails_closed(documents, anchor):
    with pytest.raises(RuntimeError, match="search city"):
        _verify_custom_anchor(documents, anchor)


def test_city_identity_exact_numeric_and_accented_name():
    documents = [_custom_search_document(city_id="123456", city_name="Montréal, Québec")]
    assert _verify_custom_anchor(documents, "123456") == ("123456", "Montréal, Québec")
    assert _verify_custom_anchor(documents, "montreal") == ("123456", "Montréal, Québec")


def test_final_city_verification_rejects_location_change(tmp_path, monkeypatch):
    original = _FakeLocator.all_text_contents
    reads = [0]

    async def changed_city(self):
        reads[0] += 1
        if reads[0] > 1:
            return [json.dumps(_custom_search_document(_listing("1"), city_name="Montreal"))]
        return await original(self)

    monkeypatch.setattr(_FakeLocator, "all_text_contents", changed_city)
    with pytest.raises(RuntimeError, match="search city"):
        _run_fake_custom_search(tmp_path, monkeypatch, _custom_search_document(_listing("1")))
