import math

import pytest

from scout.geography import coordinate_pair, distance_km, within_radius


def test_known_distances_and_radius_boundary():
    montreal = (45.5038, -73.5744)
    ottawa = (45.4215, -75.6972)
    innisfil = (44.300, -79.616)
    assert 160 < distance_km(montreal, ottawa) < 170
    assert distance_km(montreal, innisfil) > 450
    assert distance_km(montreal, montreal) == 0
    assert within_radius(20, 20)
    assert not within_radius(20.00001, 20)
    assert not within_radius(distance_km(montreal, ottawa), 20)


@pytest.mark.parametrize("value", [None, True, False, "10", -1, math.nan, math.inf, -math.inf])
def test_unknown_or_invalid_distance_never_local(value):
    assert not within_radius(value, 20)


@pytest.mark.parametrize(
    "lat,lon", [(91, 0), (0, 181), (math.nan, 0), (True, 0), (0, "10"), (0, math.inf)]
)
def test_invalid_coordinates(lat, lon):
    assert coordinate_pair(lat, lon) is None


def test_zero_coordinates_and_dateline_are_valid():
    assert coordinate_pair(0, 0) == (0.0, 0.0)
    assert 20 < distance_km((0, 179.9), (0, -179.9)) < 23
    assert distance_km(None, (0, 0)) is None
