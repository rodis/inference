"""Capability derivers — the enricher seam (ADR 0001, re-established).

A capability is a structured fact an event carries (see `inference.event.Capability`).
Each is derived from the event's **full source events** by a small registered function,
so capabilities scale by *addition*: write a deriver, register it, list the capability in
a definition's `capabilities:` — no change to the shaper or the router. This mirrors the
engine registry (detection) on the shaping side.

A deriver takes the source event records and returns a **fragment** of `InferredEvent`
fields to merge onto the emitted event (e.g. `{"interval": Interval(...)}`). Deriving over
full source bodies (not the trimmed `derived_from` lineage) is deliberate: a future `geo`
or `amount` capability needs message fields that the lineage projection doesn't carry.

Import-clean (pure Python + the domain model); importing this module registers the
built-ins, the same side-effect pattern as `inference.engines`.
"""

from collections.abc import Callable

from inference.event import Capability, Interval, Place
from inference.geo import haversine_m

# capability → deriver(sources) -> fragment of InferredEvent fields
_DERIVERS: dict[Capability, Callable[[list[dict]], dict]] = {}


def register_capability(capability: Capability):
    """Decorator registering a deriver for `capability`."""

    def _wrap(fn: Callable[[list[dict]], dict]) -> Callable[[list[dict]], dict]:
        _DERIVERS[capability] = fn
        return fn

    return _wrap


def derive_capability(capability: Capability, sources: list[dict]) -> dict:
    """Run the registered deriver, returning the InferredEvent-field fragment it produces."""
    try:
        deriver = _DERIVERS[capability]
    except KeyError:
        raise RuntimeError(
            f"No deriver registered for capability '{capability}'. "
            f"Registered: {sorted(c.value for c in _DERIVERS)}"
        ) from None
    return deriver(sources)


def _source_timestamps(sources: list[dict]) -> list[int]:
    return [(s.get("message") or {})["timestamp"] for s in sources]


@register_capability(Capability.INTERVAL)
def _interval(sources: list[dict]) -> dict:
    """The interval spans the lineage's extent — earliest source to latest. Pure function
    of the evidence; no engine-specific knowledge, so any event declaring INTERVAL gets it
    the same way. Callers guarantee non-empty sources (a declared capability with none is a
    misconfiguration)."""
    timestamps = _source_timestamps(sources)
    return {"interval": Interval(started_at=min(timestamps), ended_at=max(timestamps))}


# --- place: reference data ------------------------------------------------------
#
# Known places are DATA, loaded from Neon at startup and injected here by the adapter
# (`inference.runtime.places`), exactly as geofence regions are. It lives at module level
# because a capability deriver's signature is `(sources) -> fragment`: the deriver stays a
# pure function of (evidence, reference data), and the reference data is set once, explicitly,
# by the composition root — never fetched from inside the core.
_PLACE_BOOK: list[dict] = []


def set_place_book(places: list[dict]) -> None:
    """Install the known-place list: dicts of `name`, `lat`, `lon`, `radius_m`. Replaces any
    previous book (so a restart picks up edits); an empty book simply means no stay gets a
    label, which is a degraded mode rather than an error."""
    global _PLACE_BOOK
    _PLACE_BOOK = list(places)


def place_book() -> list[dict]:
    """The installed known-place list (read-only view for diagnostics/tests)."""
    return list(_PLACE_BOOK)


def _match_place(lat: float, lon: float) -> tuple[dict, float] | None:
    """Nearest known place containing this point, as (place_row, distance_m).

    Containment uses each place's own `radius_m`, so a big region and a small shop can
    coexist in one book, and the NEAREST match wins when radii overlap (a shop inside a
    declared district labels the shop, not the district).

    Returns the whole row rather than just its name so every reference-data field the row
    carries (`everyday` today) reaches the fragment without a second lookup.
    """
    hits = []
    for p in _PLACE_BOOK:
        try:
            dist = haversine_m(lat, lon, float(p["lat"]), float(p["lon"]))
        except (KeyError, TypeError, ValueError):
            continue                                   # a malformed row must not break shaping
        if dist <= float(p.get("radius_m", 0)):
            hits.append((p, dist))
    return min(hits, key=lambda h: h[1]) if hits else None


@register_capability(Capability.PLACE)
def _place(sources: list[dict]) -> dict:
    """Where the event happened: the centroid of its contributing fixes, plus the known place
    that contains it (if any).

    Sources without coordinates are skipped, and an event whose evidence carries none at all
    yields NO place fragment rather than a fabricated point — declaring the capability on a
    definition whose sources aren't geo is then visibly a no-op instead of a lie.
    """
    fixes = []
    for s in sources:
        msg = s.get("message") or {}
        lat, lon = msg.get("lat"), msg.get("lon")
        if lat is None or lon is None:
            continue
        try:
            fixes.append((float(lat), float(lon)))
        except (TypeError, ValueError):
            continue
    if not fixes:
        return {}

    clat = sum(f[0] for f in fixes) / len(fixes)
    clon = sum(f[1] for f in fixes) / len(fixes)
    spread = max((haversine_m(clat, clon, la, lo) for la, lo in fixes), default=0.0)
    match = _match_place(clat, clon)
    return {"place": Place(
        lat=clat, lon=clon, spread_m=round(spread, 1),
        label=str(match[0].get("name", "")) if match else None,
        distance_m=round(match[1], 1) if match else None,
        everyday=bool(match[0].get("everyday", False)) if match else None,
    )}
