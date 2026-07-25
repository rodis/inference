"""Capability derivers: interval derives generically from the source events' extent."""

import pytest

from inference.event import Capability
from inference import capabilities
from inference.capabilities import derive_capability


def _src(ts, id="i"):
    return {"message": {"id": id, "name": "x", "timestamp": ts}}


def test_interval_spans_min_to_max_of_sources():
    frag = derive_capability(Capability.INTERVAL, [_src(300), _src(100), _src(200)])
    iv = frag["interval"]
    assert (iv.started_at, iv.ended_at, iv.duration_seconds) == (100, 300, 200)


def test_unknown_capability_raises():
    with pytest.raises(RuntimeError):
        derive_capability("not_a_capability", [_src(1)])


# --- place ----------------------------------------------------------------------

def _fix(lat, lon, ts=100):
    return {"message": {"id": "i", "name": "location_ping", "timestamp": ts, "lat": lat, "lon": lon}}


# Two real home fixes ~9m apart, and a shop ~4km away.
_H1, _H2 = (47.20694, 8.57468), (47.20702, 8.57472)
_SHOP = (47.194887, 8.523353)


def test_place_derives_centroid_and_spread_without_any_reference_data():
    """The centroid half is pure: an event at an unlisted place still knows WHERE it was."""
    capabilities.set_place_book([])
    pl = derive_capability(Capability.PLACE, [_fix(*_H1), _fix(*_H2)])["place"]
    assert pytest.approx(pl.lat, abs=1e-5) == 47.20698
    assert pytest.approx(pl.lon, abs=1e-5) == 8.574700
    assert 0 < pl.spread_m < 10                      # half the ~9m separation
    assert pl.label is None and pl.distance_m is None


def test_place_labels_a_stay_inside_a_known_place():
    capabilities.set_place_book([{"name": "Home", "lat": 47.206985, "lon": 8.574798, "radius_m": 80}])
    pl = derive_capability(Capability.PLACE, [_fix(*_H1), _fix(*_H2)])["place"]
    assert pl.label == "Home" and pl.distance_m < 20


def test_place_carries_the_everyday_flag_from_the_matched_place():
    """`everyday` marks the place you LIVE in, whose stays have no natural boundaries — the
    runtime still derives them, and the flag lets a consumer skip them without the engine
    having an opinion about what to draw."""
    capabilities.set_place_book([
        {"name": "Home", "lat": 47.206985, "lon": 8.574798, "radius_m": 80, "everyday": True},
        {"name": "Shop", "lat": 47.194887, "lon": 8.523353, "radius_m": 80, "everyday": False},
    ])
    assert derive_capability(Capability.PLACE, [_fix(*_H1)])["place"].everyday is True
    assert derive_capability(Capability.PLACE, [_fix(*_SHOP)])["place"].everyday is False


def test_place_everyday_defaults_to_false_for_a_row_that_omits_it():
    """The column is new; a book assembled without it must not make every place everyday."""
    capabilities.set_place_book([{"name": "Home", "lat": 47.206985, "lon": 8.574798, "radius_m": 80}])
    assert derive_capability(Capability.PLACE, [_fix(*_H1)])["place"].everyday is False


def test_place_everyday_is_none_when_nothing_matched():
    """An unlabelled stay makes no claim either way — it isn't 'not everyday', it's unknown."""
    capabilities.set_place_book([{"name": "Home", "lat": 47.206985, "lon": 8.574798,
                                 "radius_m": 80, "everyday": True}])
    assert derive_capability(Capability.PLACE, [_fix(*_SHOP)])["place"].everyday is None


def test_place_leaves_an_unknown_place_unlabelled_but_located():
    capabilities.set_place_book([{"name": "Home", "lat": 47.206985, "lon": 8.574798, "radius_m": 80}])
    pl = derive_capability(Capability.PLACE, [_fix(*_SHOP)])["place"]
    assert pl.label is None                          # 4km away -> no match
    assert pytest.approx(pl.lat, abs=1e-6) == _SHOP[0]   # ...but still located


def test_place_nearest_match_wins_when_radii_overlap():
    """A shop inside a declared district must label the shop, not the district."""
    capabilities.set_place_book([
        {"name": "District", "lat": 47.2000, "lon": 8.5500, "radius_m": 5000},
        {"name": "Shop", "lat": 47.194887, "lon": 8.523353, "radius_m": 80},
    ])
    assert derive_capability(Capability.PLACE, [_fix(*_SHOP)])["place"].label == "Shop"


def test_place_absent_when_no_source_carries_coordinates():
    """Declaring the capability on a non-geo definition is a visible no-op, not a fake point."""
    capabilities.set_place_book([])
    assert derive_capability(Capability.PLACE, [_src(100), _src(200)]) == {}


def test_place_survives_a_malformed_reference_row():
    capabilities.set_place_book([{"name": "Broken"}, {"name": "Home", "lat": 47.206985,
                                                      "lon": 8.574798, "radius_m": 80}])
    assert derive_capability(Capability.PLACE, [_fix(*_H1)])["place"].label == "Home"
