"""Naming a stay: the pure half of turning "what shop was that?" into a `regions` POI row.

A stay always knows *where* it happened — `place.lat/lon/spread_m` is a pure function of its
own fixes — and only sometimes knows *what* that place is, because the label is a lookup
against the POI registry (ADR 0007). Naming one is therefore not a correction of the
geometry; it is adding the missing reference-data row, at coordinates we already have.

**The geocoder suggests, the stay decides where.** A search hit contributes its *name* and its
*categories* and nothing else: the row is written at the stay's own centroid. Nominatim
returns a building's canonical point, which for a shop in a block sits wherever the polygon's
centre falls — metres to tens of metres from where a phone actually sat, and biased the same
way every visit. The centroid is the fact we measured; the name is the fact we looked up.

Everything here is stdlib and free of I/O so it can be unit-tested: `app.py` needs fastapi and
psycopg, and CI's python job installs neither (see `tests/test_task_contract.py`).
"""

import math

# --- categories ------------------------------------------------------------------------------
# OSM's (class, type) pair -> the ordered category vocabulary the `regions` rows already use
# and `view.ts::PLACE_ICON` already draws. Ordered because the row's list is ordered: the first
# entry with a glyph wins, so a Konditorei must read ['bakery', 'cafe'] and a filling station
# with a shop ['fuel', 'shop'] — primary first, the rest for filtering.
#
# An unmapped type is NOT dropped: it passes through as its own bare OSM type. The vocabulary is
# user-owned data and an unknown category simply doesn't get a glyph yet, which is the failure
# `placeIcon` is already written to absorb. Inventing a mapping table that must be complete
# would make a new kind of place a code change; this way it is a row.
_TYPE_CATEGORIES: dict[str, list[str]] = {
    "veterinary": ["vet"],
    "bakery": ["bakery", "cafe"],
    "pastry": ["bakery", "cafe"],
    "confectionery": ["bakery", "cafe"],
    "cafe": ["cafe"],
    "coffee": ["cafe"],
    "fuel": ["fuel"],
    "charging_station": ["fuel"],
    "fast_food": ["fast_food", "restaurant"],
    "restaurant": ["restaurant"],
    "pub": ["restaurant"],
    "bar": ["restaurant"],
    "supermarket": ["shop"],
    "convenience": ["shop"],
}

# `class=shop` means "this is a retail premises" whatever its type, so a shop type we have no
# mapping for still earns the generic glyph — appended, never leading, so `supermarket` keeps
# its own reading first if it ever gains one.
_SHOP_CLASSES = {"shop"}

# Hits that say *where* something is and nothing about what it is — the address nodes, street
# lines and building outlines an address search returns. They map to NO category, because the
# alternative is worse than useless: two of these were caught against live Nominatim on the same
# afternoon. `place=house` fell through as `house`, which `PLACE_ICON` draws with the House glyph
# reserved for `home`, so searching a shop's street address quietly claimed the shop was where
# you live; and `highway=primary` fell through as `primary`, filing a street under a road
# classification. A category the vocabulary can't express is better left unsaid — `everyday` is a
# checkbox, not an inference.
#
# Keyed on the *class* wherever one is positional by nature, because the type list underneath it
# is open-ended: `highway` alone spans primary/secondary/tertiary/residential/service/footway/…,
# and enumerating them means the next unlisted one becomes a category again.
_POSITIONAL_CLASSES = {"highway", "place", "boundary", "building", "landuse"}
_POSITIONAL_TYPES = {"house", "houses", "residential", "apartments", "building", "yes",
                     "address", "postcode", "street", "road", "neighbourhood", "suburb"}


def categories_for(osm_class: str | None, osm_type: str | None) -> list[str]:
    """The ordered category list for one OSM (class, type) pair.

    Falls through to the bare type for anything unmapped, and appends `shop` for retail — so a
    `shop=greengrocer` becomes `['greengrocer', 'shop']` and draws the store glyph today while
    leaving room for a greengrocer glyph later.
    """
    kind = (osm_type or "").strip().lower()
    cls = (osm_class or "").strip().lower()
    if cls in _POSITIONAL_CLASSES or kind in _POSITIONAL_TYPES:
        cats: list[str] = []
    else:
        cats = list(_TYPE_CATEGORIES.get(kind, [kind] if kind else []))
    if cls in _SHOP_CLASSES and "shop" not in cats:
        cats.append("shop")
    return cats


# --- geometry --------------------------------------------------------------------------------
# A local copy rather than an import of `inference.geo`: the dashboard image's Docker context is
# `dashboard/`, so `src/` is unreachable from here — the same build boundary that made
# `processes.json` a generated file. Six lines of haversine is the cheaper side of that trade.
EARTH_RADIUS_M = 6371000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


# How far around the stay's centroid a name search is allowed to look. Wide enough that a
# building's canonical point and its address node both fall inside (the two can differ by a
# block), narrow enough that typing "Coop" doesn't offer the branch in the next town — which is
# the whole failure this bounding prevents, since a wrong pick writes a permanent row.
SEARCH_BOX_M = 500.0


def viewbox(lat: float, lon: float, radius_m: float = SEARCH_BOX_M) -> str:
    """Nominatim `viewbox` (left,top,right,bottom) for a box of `radius_m` around a point.

    Longitude degrees shrink with latitude, so the east-west half-width is divided by
    cos(lat); at 47°N that is a factor of ~1.47, and skipping it would make the box a third
    narrower than tall. Clamped near the poles where the division explodes.
    """
    dlat = math.degrees(radius_m / EARTH_RADIUS_M)
    dlon = math.degrees(radius_m / (EARTH_RADIUS_M * max(math.cos(math.radians(lat)), 0.01)))
    return f"{lon - dlon:.6f},{lat + dlat:.6f},{lon + dlon:.6f},{lat - dlat:.6f}"


# --- the row ---------------------------------------------------------------------------------
# The radius a named place gets by default, and the bounds it is held to. The floor exists
# because a stay's spread can be a handful of metres on a dense sample, and a 6m POI would fail
# to contain the *next* visit to the same shop — the row has to cover the scatter of every
# future stay, not just the one being named. The ceiling stops a sparse, smeared stay from
# minting a POI so wide it swallows its neighbours: `_match_place` takes the nearest containing
# row, so an oversized one doesn't hide a smaller shop inside it, but it does start claiming
# stays that happened next door. Both ends match the 50–80m the hand-made rows already use.
RADIUS_MIN_M, RADIUS_MAX_M = 30.0, 150.0
DEFAULT_RADIUS_MIN_M, DEFAULT_RADIUS_MAX_M = 50.0, 80.0


def default_radius_m(spread_m: float | None) -> float:
    """The radius to offer for a stay whose fixes scattered over `spread_m`."""
    try:
        spread = float(spread_m)
    except (TypeError, ValueError):
        return DEFAULT_RADIUS_MIN_M
    return round(min(max(spread, DEFAULT_RADIUS_MIN_M), DEFAULT_RADIUS_MAX_M), 1)


def clean_name(name: str) -> str:
    """The name as stored: trimmed, inner whitespace collapsed. Empty is the caller's error."""
    return " ".join((name or "").split())


def osm_class(hit: dict) -> str | None:
    """The hit's OSM class, under whichever key this response format used.

    `format=jsonv2` renames `class` to `category`; `format=json` keeps `class`. Reading only one
    of them is a silent failure rather than a loud one — every class-based rule below simply
    sees `None` and stops firing, which is exactly what happened until a live search for
    "Bahnhofstrasse" came back filed under `primary`. Both keys are read so the mapping cannot
    depend on which format the caller asked for.
    """
    return hit.get("category") or hit.get("class") or None


def candidate(hit: dict, lat: float, lon: float) -> dict | None:
    """One Nominatim hit as a suggestion: `{name, display_name, categories, lat, lon,
    distance_m}`, or `None` if it carries no usable position.

    `distance_m` is measured from the *stay's* centroid, not between hits — it is the column
    that answers "is this the one I was actually standing in?", which is the only question the
    list has to settle. A hit missing a name falls back to the first comma-separated part of
    `display_name`, which for an address search is the street-and-number and is exactly what
    someone typing an address means to store.
    """
    try:
        hlat, hlon = float(hit["lat"]), float(hit["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    display = str(hit.get("display_name") or "").strip()
    name = clean_name(str(hit.get("name") or "")) or clean_name(display.split(",")[0])
    if not name:
        return None
    return {
        "name": name,
        "display_name": display,
        "categories": categories_for(osm_class(hit), hit.get("type")),
        "lat": hlat,
        "lon": hlon,
        "distance_m": round(haversine_m(lat, lon, hlat, hlon), 1),
    }


def candidates(hits: list[dict], lat: float, lon: float, limit: int = 8) -> list[dict]:
    """Nominatim's hits as suggestions, nearest to the stay first, de-duplicated by name.

    Nearest-first rather than Nominatim's own relevance order because the query is anchored to
    a point we are certain about: among things called "Coop" inside a 500m box, the one 40m away
    is the one you stood in. De-duplication keeps the nearest of a repeated name — OSM commonly
    carries a shop as both a node and a building polygon, and offering the same name twice makes
    the list look broken while adding nothing to choose between.
    """
    out: dict[str, dict] = {}
    for hit in hits:
        c = candidate(hit, lat, lon)
        if c is None:
            continue
        key = c["name"].casefold()
        if key not in out or c["distance_m"] < out[key]["distance_m"]:
            out[key] = c
    return sorted(out.values(), key=lambda c: c["distance_m"])[:limit]
