"""Naming a stay: the pure half (`dashboard/places.py`).

Loaded by path rather than imported, for the same reason `test_task_contract.py` reads
`app.py` as text: the dashboard image is built with `dashboard/` as its Docker context, so it
is not a package on this path. Unlike `app.py`, this module is stdlib-only and *can* be
executed here, which is precisely why the category mapping and the geometry live in it.
"""

import importlib.util
import math
import pathlib

import pytest

_PATH = pathlib.Path(__file__).resolve().parents[1] / "dashboard" / "places.py"
_spec = importlib.util.spec_from_file_location("dashboard_places", _PATH)
places = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(places)


# The vocabulary the `regions` rows already use and `view.ts::PLACE_ICON` already draws. If a
# mapping here produces something outside it, the row still stores fine but never gets a glyph.
GLYPHED = {"fuel", "cafe", "bakery", "vet", "home", "shop", "fast_food", "restaurant"}


@pytest.mark.parametrize("osm_class,osm_type,expected", [
    ("shop", "bakery", ["bakery", "cafe", "shop"]),      # a Konditorei: croissant, then shop
    ("amenity", "cafe", ["cafe"]),
    ("amenity", "veterinary", ["vet"]),                  # renamed to the vocabulary's word
    ("amenity", "fuel", ["fuel"]),
    ("amenity", "fast_food", ["fast_food", "restaurant"]),
    ("shop", "supermarket", ["shop"]),
])
def test_known_types_map_into_the_drawn_vocabulary(osm_class, osm_type, expected):
    cats = places.categories_for(osm_class, osm_type)
    assert cats == expected
    assert cats[0] in GLYPHED, "the primary category must be one the UI can draw"


def test_unmapped_shop_type_still_reads_as_a_shop():
    """An unknown retail type passes through and picks up the generic glyph — appended, so a
    future `greengrocer` icon would take precedence without touching this table."""
    assert places.categories_for("shop", "greengrocer") == ["greengrocer", "shop"]


@pytest.mark.parametrize("osm_type", ["house", "yes", "building", "residential"])
def test_address_hits_carry_no_category(osm_type):
    """The regression this table exists for: `place=house` fell through as `house`, which the
    UI draws with the House glyph reserved for `home` — so searching a shop's street address
    claimed the shop was where you live."""
    assert places.categories_for("place", osm_type) == []


@pytest.mark.parametrize("osm_type", ["primary", "secondary", "residential", "service",
                                      "footway", "unclassified", "living_street"])
def test_a_street_is_not_a_kind_of_place(osm_type):
    """Caught against live Nominatim: searching "Bahnhofstrasse" returned `highway=primary`,
    which fell through and filed the street under a road classification. Keyed on the CLASS
    because the type list under `highway` is open-ended — enumerating it means the next
    unlisted road type becomes a category again."""
    assert places.categories_for("highway", osm_type) == []


def test_unknown_class_and_type_is_empty_not_a_crash():
    assert places.categories_for(None, None) == []
    assert places.categories_for("", "") == []


def test_viewbox_is_wider_in_longitude_than_in_latitude():
    """Longitude degrees shrink with latitude; at 47°N the box must be ~1.47x wider in degrees
    to be square in metres. Skipping the cos(lat) division makes it a third narrow."""
    lat, lon = 47.1723, 8.5148
    left, top, right, bottom = (float(v) for v in places.viewbox(lat, lon).split(","))
    assert left < lon < right and bottom < lat < top
    assert (right - left) / (top - bottom) == pytest.approx(
        1 / math.cos(math.radians(lat)), rel=1e-3)


def test_viewbox_corner_is_the_requested_distance_away():
    lat, lon = 47.1723, 8.5148
    left, top, _, _ = (float(v) for v in places.viewbox(lat, lon, 500).split(","))
    assert places.haversine_m(lat, lon, lat, left) == pytest.approx(500, rel=0.01)
    assert places.haversine_m(lat, lon, top, lon) == pytest.approx(500, rel=0.01)


@pytest.mark.parametrize("spread,expected", [
    (67.0, 67.0),      # inside the band: the stay's own scatter is the best estimate
    (12.0, 50.0),      # floored — a 12m POI would miss the next visit to the same shop
    (140.0, 80.0),     # capped — a smeared stay must not mint a row that claims its neighbours
    (None, 50.0),      # no spread on the event: the floor, never zero
    ("nonsense", 50.0),
])
def test_default_radius_stays_in_the_band_the_hand_made_rows_use(spread, expected):
    assert places.default_radius_m(spread) == expected


def test_a_saved_radius_may_be_wider_than_the_default_but_is_still_bounded():
    """The default band is advice; the accepted band is the guardrail. They are different
    numbers on purpose — you can widen a place you know is big, but not without limit."""
    assert places.RADIUS_MIN_M < places.DEFAULT_RADIUS_MIN_M
    assert places.DEFAULT_RADIUS_MAX_M < places.RADIUS_MAX_M


def test_clean_name_trims_and_collapses():
    assert places.clean_name("  Coop   Pronto \n Baar ") == "Coop Pronto Baar"
    assert places.clean_name("") == ""
    assert places.clean_name(None) == ""


@pytest.mark.parametrize("hit,expected", [
    ({"category": "highway"}, "highway"),        # format=jsonv2 — what the endpoint asks for
    ({"class": "highway"}, "highway"),           # format=json — the other spelling
    ({"category": "shop", "class": "ignored"}, "shop"),
    ({}, None),
])
def test_the_osm_class_is_read_under_either_key(hit, expected):
    """`format=jsonv2` renames `class` to `category`. Reading only one key fails *silently* —
    every class-based rule sees None and stops firing — which is how a live search for
    "Bahnhofstrasse" came back categorised as `primary` despite the highway rule existing."""
    assert places.osm_class(hit) == expected


def test_a_jsonv2_street_hit_is_categoryless_end_to_end():
    """The regression as the endpoint actually sees it: through `candidate`, with jsonv2's keys."""
    (got,) = places.candidates([{
        "name": "Bahnhofstrasse", "lat": "47.1750", "lon": "8.5148",
        "category": "highway", "type": "primary",
        "display_name": "Bahnhofstrasse, Zug, Schweiz",
    }], 47.1723, 8.5148)
    assert got["categories"] == []


def _hit(name, lat, lon, cls="shop", typ="supermarket", display=None):
    return {"name": name, "lat": str(lat), "lon": str(lon), "category": cls, "type": typ,
            "display_name": display or f"{name}, Bahnhofstrasse 1, Zug"}


def test_candidates_are_ordered_by_distance_from_the_stay_not_by_relevance():
    """The query is anchored to a point we are certain about, so among things called "Coop"
    inside the box, the one 27m away is the one you stood in."""
    lat, lon = 47.1723, 8.5148
    hits = [_hit("Far Coop", 47.1760, 8.5190), _hit("Near Coop", 47.1725, 8.5150)]
    got = places.candidates(hits, lat, lon)
    assert [c["name"] for c in got] == ["Near Coop", "Far Coop"]
    assert got[0]["distance_m"] < got[1]["distance_m"]


def test_the_same_name_twice_collapses_to_the_nearer_one():
    """OSM commonly carries a shop as both a node and a building polygon. Offering it twice
    looks broken and adds nothing to choose between."""
    lat, lon = 47.1723, 8.5148
    hits = [_hit("Coop", 47.1729, 8.5155), _hit("coop", 47.1725, 8.5150)]
    got = places.candidates(hits, lat, lon)
    assert len(got) == 1
    assert got[0]["distance_m"] == pytest.approx(26.9, abs=1.0)


def test_a_nameless_address_hit_falls_back_to_its_street_and_number():
    """Typing an address should be able to name a place, and what you meant to store is the
    first part of the display name, not the whole postal string."""
    lat, lon = 47.1723, 8.5148
    hit = {"lat": "47.1720", "lon": "8.5145", "class": "place", "type": "house",
           "display_name": "Bahnhofstrasse 3, 6300 Zug, Schweiz"}
    (got,) = places.candidates([hit], lat, lon)
    assert got["name"] == "Bahnhofstrasse 3"
    assert got["categories"] == []


def test_hits_without_coordinates_are_skipped_not_fatal():
    """One malformed hit must not lose the rest of the list — the same degrade-don't-die rule
    the capability derivers follow for a bad source."""
    lat, lon = 47.1723, 8.5148
    hits = [{"name": "Broken"}, {"name": "NoLat", "lon": "8.5"}, _hit("Good", 47.1725, 8.5150)]
    got = places.candidates(hits, lat, lon)
    assert [c["name"] for c in got] == ["Good"]


def test_candidates_respect_the_limit():
    lat, lon = 47.1723, 8.5148
    hits = [_hit(f"Shop {i}", 47.1723 + i / 10000, 8.5148) for i in range(20)]
    assert len(places.candidates(hits, lat, lon, limit=5)) == 5
