"""Asking-price estimates and explicitly observed listing lifecycles."""

import hashlib
import json
import math
import time
from dataclasses import asdict
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .geography import within_radius
from .models import COUNTRIES, matches, normalize

HISTORY_DAYS = 90
HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_observations(
 scope TEXT NOT NULL, source TEXT NOT NULL, listing_id TEXT NOT NULL,
 payload TEXT NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
 first_active REAL, last_active REAL, sold_seen REAL, last_active_price TEXT,
 PRIMARY KEY(scope,source,listing_id));
CREATE INDEX IF NOT EXISTS price_observations_age ON price_observations(last_seen);
"""


def scope_key(spec):
    # Labels are cosmetic; filters, country and radius change the comparison pool.
    value = spec.model_dump(mode="json")
    value["pricing"].pop("label")
    value["query"] = normalize(value["query"])
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def amount(item, currency):
    if item.get("currency") != currency or item.get("price_kind", "price") != "price":
        return None
    try:
        value = Decimal(str(item.get("price")))
    except (InvalidOperation, ValueError):
        return None
    # Free/placeholder values do not provide usable paid-item comparables.
    # Bound precision/range to keep malformed upstream values out of charts and arithmetic.
    if not value.is_finite() or not 0 < value <= 1_000_000_000 or value.adjusted() < -2:
        return None
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _distance_state(item, radius_km):
    """Classify a listing distance before it enters a local price report.

    Listing distances are provider data.  A boolean is an integer subclass, so
    reject it explicitly instead of accidentally treating ``True`` as 1 km.
    Strings and non-finite values are unknown rather than outside the radius.
    """

    distance = item.get("distance_km")
    if isinstance(distance, bool) or not isinstance(distance, (int, float)):
        return "unknown"
    if not math.isfinite(distance) or distance < 0:
        return "unknown"
    return "local" if within_radius(distance, radius_km) else "outside"


def money(value):
    return (
        str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) if value is not None else None
    )


def percentile(values, fraction):
    index = (len(values) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (index - lower)


def record_observations(db, spec, listings, now):
    key = scope_key(spec)
    db.execute("DELETE FROM price_observations WHERE last_seen < ?", (now - HISTORY_DAYS * 86400,))
    for listing in listings:
        if not matches(spec, listing):
            continue
        if (
            _distance_state(
                {"distance_km": getattr(listing, "distance_km", None)},
                spec.pricing.radius_km,
            )
            != "local"
        ):
            continue
        payload = asdict(listing)
        old = db.execute(
            "SELECT * FROM price_observations WHERE scope=? AND source=? AND listing_id=?",
            (key, listing.source, listing.id),
        ).fetchone()
        old_is_local = (
            old is not None
            and _distance_state(json.loads(old["payload"]), spec.pricing.radius_km) == "local"
        )
        # A previously unverified/outside row cannot establish the start of a
        # local lifecycle.  Reset it before accepting this verified sighting.
        first_seen = old["first_seen"] if old and old_is_local else now
        first_active = old["first_active"] if old and old_is_local else None
        last_active = old["last_active"] if old and old_is_local else None
        sold_seen = old["sold_seen"] if old and old_is_local else None
        last_price = old["last_active_price"] if old and old_is_local else None
        if listing.status == "active":
            if sold_seen is not None:  # Explicit relisting: start a new observed lifecycle.
                first_seen = now
                first_active = None
            first_active = first_active if first_active is not None else now
            last_active, sold_seen = now, None
            last_price = money(amount(payload, COUNTRIES[spec.countries[0]]))
        elif listing.status == "sold":
            sold_seen = sold_seen if sold_seen is not None else now
        elif listing.status == "pending" and sold_seen is not None:
            # A sold->pending reversal no longer supports a sold claim.
            first_seen, first_active, last_active, sold_seen, last_price = (
                now,
                None,
                None,
                None,
                None,
            )
        db.execute(
            "INSERT OR REPLACE INTO price_observations VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                key,
                listing.source,
                listing.id,
                json.dumps(payload),
                first_seen,
                now,
                first_active,
                last_active,
                sold_seen,
                last_price,
            ),
        )


def histogram(values):
    if not values:
        return []
    low, high = min(values), max(values)
    if low == high:
        return [{"low": money(low), "high": money(high), "count": len(values)}]
    bins = min(8, len(values))
    width = (high - low) / bins
    counts = [0] * bins
    for value in values:
        counts[min(int((value - low) / width), bins - 1)] += 1
    return [
        {"low": money(low + i * width), "high": money(low + (i + 1) * width), "count": count}
        for i, count in enumerate(counts)
    ]


def price_report(db, spec, items, search):
    currency = COUNTRIES[spec.countries[0]]
    distances = [(_distance_state(item, spec.pricing.radius_km), item) for item in items]
    outside_radius_count = sum(state == "outside" for state, _ in distances)
    unknown_distance_count = sum(state == "unknown" for state, _ in distances)
    active = [
        item
        for state, item in distances
        if state == "local" and item.get("status", "active") == "active"
    ]
    usable = [(item, amount(item, currency)) for item in active]
    usable = [(item, price) for item, price in usable if price is not None]
    values = sorted(price for _, price in usable)
    sold_items, speed_points = [], []
    history_outside_radius = False
    history_unknown_distance = False
    for row in db.execute(
        "SELECT * FROM price_observations WHERE scope=? AND sold_seen IS NOT NULL "
        "AND last_seen>=? AND sold_seen>=? ORDER BY sold_seen DESC,listing_id",
        (scope_key(spec), time.time() - HISTORY_DAYS * 86400, time.time() - HISTORY_DAYS * 86400),
    ):
        item = json.loads(row["payload"])
        history_state = _distance_state(item, spec.pricing.radius_km)
        if history_state != "local":
            # Older records predate verified listing coordinates.  Keep them
            # out of the local report even when their scope key still matches.
            if history_state == "outside":
                history_outside_radius = True
            else:
                history_unknown_distance = True
            continue
        if item.get("status") != "sold":
            continue
        days = (
            round(max(0, row["sold_seen"] - row["first_active"]) / 86400, 2)
            if row["first_active"] is not None
            else None
        )
        sold_items.append(
            {
                **item,
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "sold_seen": row["sold_seen"],
                "observed_days": days,
                "last_active_price": row["last_active_price"],
                "last_active_currency": currency if row["last_active_price"] is not None else None,
                "last_active": row["last_active"],
            }
        )
        if days is not None and row["last_active_price"] is not None:
            speed_points.append(
                {
                    "id": item["id"],
                    "title": item["title"],
                    "price": row["last_active_price"],
                    "days": days,
                }
            )
    warnings = [
        "Regional sample, not a complete inventory. Check seller locations and comparable condition/model.",
        f"The selected country sets the comparison currency ({currency}); it does not verify the city's country. Use a numeric Marketplace city ID for ambiguous city names.",
        "Prices are asking prices, including those on sold listings; final transaction prices are unknown.",
        "Sold items appear only when returned with an explicit sold flag. Missing listings are never counted as sold.",
        "Rescan the same product and area to observe changes. Days shown run from first active sighting to first sold sighting, not the actual listing or sale dates.",
    ]
    if len(values) < 3:
        warnings.append("At least 3 usable active comparables are needed for a price suggestion.")
    elif len(values) < 10:
        warnings.append("Small sample: review the comparables before choosing your price.")
    if search["status"] != "complete":
        warnings.append("This scan is not complete; any available estimate is provisional.")
    if outside_radius_count:
        warnings.append(
            f"{outside_radius_count} matching listing(s) were outside the requested "
            f"{spec.pricing.radius_km} km radius and were excluded from pricing and history."
        )
    if unknown_distance_count:
        warnings.append(
            f"{unknown_distance_count} matching listing(s) had no verified distance and were "
            "excluded from pricing and history; rescan to get a local estimate."
        )
    if history_outside_radius:
        warnings.append(
            f"Existing sold history outside the requested {spec.pricing.radius_km} km radius "
            "was excluded; rescan the same area to rebuild local history."
        )
    if history_unknown_distance:
        warnings.append(
            "Existing price history without a verified local distance was excluded; rescan "
            "the same area to rebuild local history."
        )
    for country in search["countries"]:
        for region in country["regions"]:
            if region["radius_km"] and region["radius_km"] != spec.pricing.radius_km:
                warnings.append(
                    f"Requested {spec.pricing.radius_km} km; Facebook applied {region['radius_km']} km."
                )
            if region["coverage_warning"]:
                warnings.append(region["coverage_warning"])
            if region["saturated"]:
                warnings.append("Facebook has more results than were collected.")
    return {
        "currency": currency,
        "sample_size": len(values),
        "minimum": money(values[0]) if values else None,
        "average": money(sum(values) / len(values)) if values else None,
        "maximum": money(values[-1]) if values else None,
        "median": money(percentile(values, Decimal("0.5"))) if values else None,
        "suggested_price": money(percentile(values, Decimal("0.5"))) if len(values) >= 3 else None,
        "quick_sale_price": money(percentile(values, Decimal("0.25")))
        if len(values) >= 3
        else None,
        "patient_price": money(percentile(values, Decimal("0.75"))) if len(values) >= 3 else None,
        "recommendation_basis": "Suggested price is the median active asking price. Lower and upper quartiles offer competitive and patient starting points; sale speed is not predicted.",
        "excluded_count": len(active) - len(usable),
        "outside_radius_count": outside_radius_count,
        "unknown_distance_count": unknown_distance_count,
        "sold_count": len(sold_items),
        "sold_items": sold_items,
        "items": [item for item, _ in usable],
        "histogram": histogram(values),
        "speed_points": speed_points,
        "warnings": list(dict.fromkeys(warnings)),
    }
