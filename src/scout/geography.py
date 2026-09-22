"""Distance checks for local price comparisons, using reported coordinates."""

import math


def coordinate_pair(latitude, longitude):
    values = (latitude, longitude)
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
        return None
    if not all(math.isfinite(value) for value in values):
        return None
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        return None
    return float(latitude), float(longitude)


def distance_km(center, point):
    """Great-circle distance; unknown or malformed coordinates stay unknown."""
    if center is None or point is None:
        return None
    lat1, lon1 = map(math.radians, center)
    lat2, lon2 = map(math.radians, point)
    value = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 6371.0088 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, value))))


def within_radius(distance, radius):
    return (
        not isinstance(distance, bool)
        and isinstance(distance, (int, float))
        and math.isfinite(distance)
        and 0 <= distance <= radius
    )
