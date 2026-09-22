"""Parse search-scoped Facebook records and the parameters actually applied."""

import re

from ..geography import coordinate_pair, distance_km
from ..models import Listing


def objects(documents):
    stack = list(reversed(documents))
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            yield value
            stack.extend(reversed(list(value.values())))
        elif isinstance(value, list):
            stack.extend(reversed(value))


def search_feeds(documents):
    return [
        obj["marketplace_search"]["feed_units"]
        for obj in objects(documents)
        if isinstance(obj.get("marketplace_search"), dict)
        and isinstance(obj["marketplace_search"].get("feed_units"), dict)
    ]


def applied_radius(documents):
    radii = [
        obj["filter_radius_km"]
        for obj in objects(documents)
        if isinstance(obj.get("filter_radius_km"), (int, float))
    ]
    return radii[-1] if radii else None


def applied_search_center(documents, radius):
    centers = {
        point
        for obj in objects(documents)
        if obj.get("filter_radius_km") == radius
        and (
            point := coordinate_pair(
                obj.get("filter_location_latitude"), obj.get("filter_location_longitude")
            )
        )
        is not None
    }
    return next(iter(centers)) if len(centers) == 1 else None


def listing_coordinates(documents, listing_id):
    # Route props can carry this listing's ID but the BUYER'S search location.
    # Only listing records with a title own seller/listing location coordinates.
    points = set()
    for obj in objects(documents):
        if str(obj.get("id")) != listing_id or not isinstance(
            obj.get("marketplace_listing_title"), str
        ):
            continue
        location = obj.get("location")
        if isinstance(location, dict):
            point = coordinate_pair(location.get("latitude"), location.get("longitude"))
            if point is not None:
                points.add(point)
    return next(iter(points)) if len(points) == 1 else None


def price_currency(price, default=None):
    if price.get("currency"):
        return price["currency"]
    text = price.get("formatted_amount", "").replace("\u00a0", " ")
    for markers, code in [
        (("CA$", "$CA", "CAD", "C$"), "CAD"),
        (("US$", "$US", "USD"), "USD"),
        (("€", "EUR"), "EUR"),
        (("AU$", "$AU", "AUD", "A$"), "AUD"),
        (("£", "GBP"), "GBP"),
    ]:
        if any(marker in text for marker in markers):
            return code
    return default


def extract_listings(documents, country, scoped=False, include_sold=False, center=None):
    currencies = [
        obj["primary_currency"]
        for obj in objects(documents)
        if obj.get("__typename") == "Marketplace" and obj.get("primary_currency")
    ]
    default_currency = currencies[-1] if currencies else None
    roots = search_feeds(documents) if scoped else documents
    found = {}
    for value in objects(roots):
        title = value.get("marketplace_listing_title")
        ident = str(value.get("id", ""))
        if not isinstance(title, str) or not title.strip() or not ident.isdigit():
            continue
        is_sold = value.get("is_sold") is True
        is_pending = value.get("is_pending") is True
        if value.get("is_hidden") or (
            value.get("is_live") is False and not (is_sold or is_pending)
        ):
            continue
        if (is_sold or is_pending) and not include_sold:
            continue
        status = "sold" if is_sold else "pending" if is_pending else "active"
        price = value.get("listing_price") or {}
        reverse = (value.get("location") or {}).get("reverse_geocode") or {}
        location = ", ".join(str(reverse[key]) for key in ("city", "state") if reverse.get(key))
        if not location and isinstance(reverse.get("city_page"), dict):
            location = reverse["city_page"].get("display_name", "")
        found[ident] = Listing(
            "facebook",
            ident,
            title,
            f"https://www.facebook.com/marketplace/item/{ident}/",
            country,
            str(price["amount"]) if price.get("amount") is not None else None,
            price_currency(price, default_currency),
            location,
            status=status,
            distance_km=distance_km(center, listing_coordinates([value], ident)),
            image_url=((value.get("primary_listing_photo") or {}).get("image") or {}).get("uri"),
        )
    return list(found.values())


def confirmed_empty(documents):
    return any(
        obj.get("__typename") == "MarketplaceSearchFeedNoResults"
        and obj.get("is_main_results_empty") is True
        and obj.get("is_relaxation_results_empty") is True
        for obj in objects(search_feeds(documents))
    )


def more_results(documents):
    feeds = search_feeds(documents)
    return bool(feeds and feeds[-1].get("page_info", {}).get("has_next_page"))


def radius_option(text):
    match = re.search(r"(\d+)\s*(kilom|km|mile)", text.casefold())
    if not match:
        return None
    return round(int(match[1]) * (1.609344 if match[2] == "mile" else 1))


def choose_radius(options, desired):
    available = [(radius_option(text), text) for text in options if radius_option(text)]
    if not available:
        raise RuntimeError("Facebook offers no supported search radius")
    covering = [(km, text) for km, text in available if km >= desired - 1]
    return min(covering) if covering else max(available)
