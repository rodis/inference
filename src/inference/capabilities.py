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

from collections import Counter
from collections.abc import Callable

from inference.event import Capability, Interval, Journey, Pause, Place, Support, Vehicle
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
    misconfiguration).

    **When the evidence contains located fixes, the span is the fixes' extent.** The span of a
    journey is the span of the *movement*, not of whatever corroborated it: `trip` carries both
    the location fixes that define where it went and (issue #41) the car boundaries that prove
    whose car it was, and those boundaries sit *outside* a correctly-measured journey — you get
    in before the phone leaves the departure cluster and get out after it enters the arrival one
    (measured: median 58 s before the start and 18 s after the end). Letting them set the bounds
    would quietly redefine `trip`'s span as get-in→get-out for own-car journeys while leaving it
    displacement-derived for borrowed ones — two meanings for one field, decided by which
    peripherals happened to fire.

    This changes nothing for any other event, which is why it is safe to state generically rather
    than as a `trip` special case: `car_trip`'s sources carry no coordinates at all and fall
    through to the full set, and `stay`'s sources are *all* located.
    """
    located = [s for s in sources
               if (s.get("message") or {}).get("lat") is not None
               and (s.get("message") or {}).get("lon") is not None]
    timestamps = _source_timestamps(located or sources)
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


def _fixes(sources: list[dict]) -> list[tuple[float, float]]:
    """The (lat, lon) of every source that carries coordinates, in source order. Malformed or
    non-geo sources are skipped rather than fatal — one bad row must not break shaping.
    """
    out = []
    for s in sources:
        msg = s.get("message") or {}
        lat, lon = msg.get("lat"), msg.get("lon")
        if lat is None or lon is None:
            continue
        try:
            out.append((float(lat), float(lon)))
        except (TypeError, ValueError):
            continue
    return out


def _place_at(lat: float, lon: float, spread_m: float) -> Place:
    """A `Place` at a point, labelled against the known-place book. The one place the
    reference-data lookup is applied, so `place` and `journey`'s endpoints label identically.
    """
    match = _match_place(lat, lon)
    return Place(
        lat=lat, lon=lon, spread_m=round(spread_m, 1),
        label=str(match[0].get("name", "")) if match else None,
        distance_m=round(match[1], 1) if match else None,
        everyday=bool(match[0].get("everyday", False)) if match else None,
    )


@register_capability(Capability.PLACE)
def _place(sources: list[dict]) -> dict:
    """Where the event happened: the centroid of its contributing fixes, plus the known place
    that contains it (if any).

    Sources without coordinates are skipped, and an event whose evidence carries none at all
    yields NO place fragment rather than a fabricated point — declaring the capability on a
    definition whose sources aren't geo is then visibly a no-op instead of a lie.
    """
    fixes = _fixes(sources)
    if not fixes:
        return {}

    clat = sum(f[0] for f in fixes) / len(fixes)
    clon = sum(f[1] for f in fixes) / len(fixes)
    spread = max((haversine_m(clat, clon, la, lo) for la, lo in fixes), default=0.0)
    return {"place": _place_at(clat, clon, spread)}


@register_capability(Capability.JOURNEY)
def _journey(sources: list[dict]) -> dict:
    """Where the event went: two labelled endpoints, two distances, and the mode.

    Endpoints come from the **event-time order** of the sources, not their list order. The
    order an engine happened to append in is an implementation detail, whereas the earliest
    and latest fix are facts about the evidence — the same reasoning that has `_interval`
    take min/max rather than first/last.

    `path_m` sums the consecutive legs in that same order, so it measures the route actually
    sampled; sparse sampling shortens it (a cut corner), which is a known and acceptable
    under-estimate. `straight_line_m` is origin→destination and is NOT a substitute: a loop
    journey has one near zero and the other large.

    Fewer than two distinct fixes yields no fragment — a single point is not a journey, and
    fabricating one would be worse than the capability visibly not applying.
    """
    located = []
    for s in sources:
        msg = s.get("message") or {}
        lat, lon = msg.get("lat"), msg.get("lon")
        if lat is None or lon is None:
            continue
        try:
            located.append((int(msg.get("timestamp", 0)), float(lat), float(lon), msg))
        except (TypeError, ValueError):
            continue
    if len(located) < 2:
        return {}

    located.sort(key=lambda f: f[0])
    _, olat, olon, _ = located[0]
    _, dlat, dlon, _ = located[-1]
    path = sum(
        haversine_m(a[1], a[2], b[1], b[2])
        for a, b in zip(located, located[1:], strict=False)
    )
    return {"journey": Journey(
        # spread 0.0: each endpoint is one fix by construction (see `trip_window`), so it has
        # no spread — the endpoints are boundaries, not clusters.
        origin=_place_at(olat, olon, 0.0),
        destination=_place_at(dlat, dlon, 0.0),
        straight_line_m=round(haversine_m(olat, olon, dlat, dlon), 1),
        path_m=round(path, 1),
        mode=_mode([f[3] for f in located]),
    )}


@register_capability(Capability.VEHICLE)
def _vehicle(sources: list[dict]) -> dict:
    """Was this journey corroborated by something other than the movement itself?

    The classification is **structural, not by name**: a source carrying coordinates is part of
    the movement, and a source carrying none is corroboration. So this deriver reports whatever
    event names the evidence actually contained and never learns that a car boundary is called
    `got_into_the_car` — which definitions the corroboration comes from is the engine's config,
    and framework code stays free of concrete event names.

    Which corroborating sources reach this deriver is the ENGINE's decision, not this one's:
    the engine folds a mark when it lies inside the span plus its `corroboration_pad_seconds`
    (a correctly-measured journey systematically excludes both car boundaries), stretched
    across the adjacent evidence gap when `corroboration_gap_tolerant` is set (issue #46 —
    a cold-start entry or a parking-search exit falls minutes outside any sane pad, in a
    stretch the location stream cannot contradict). None of that widens `interval`, which
    derives from the located sources alone — the trap `validated_session_window` documents
    for its own fixes.

    Names are deduplicated in first-seen event-time order, so `evidence` reads chronologically
    and `confirmed` counts distinct signals rather than repeats (a lock burst while unloading
    groceries is one kind of evidence, not three).
    """
    seen: dict[str, int] = {}
    for s in sources:
        msg = s.get("message") or {}
        if msg.get("lat") is not None and msg.get("lon") is not None:
            continue                                   # part of the movement, not corroboration
        name = msg.get("name")
        if not isinstance(name, str):
            continue
        ts = msg.get("timestamp", 0)
        if name not in seen or ts < seen[name]:
            seen[name] = ts
    if not seen:
        return {}                                      # no corroboration — assert nothing
    evidence = sorted(seen, key=lambda n: (seen[n], n))
    return {"vehicle": Vehicle(evidence=evidence, confirmed=len(evidence) >= 2)}


# A pause is a cluster that held at least this long. Below it, ordinary traffic (a red light
# runs 30-90s) would flood the detail line; above the engines' settle threshold it would have
# been an arrival instead, so the band this captures is exactly the stops detection must
# ignore. The radius mirrors the geometry engines' clustering convention.
PAUSE_RADIUS_M = 60.0
PAUSE_MIN_SECONDS = 120


@register_capability(Capability.PAUSES)
def _pauses(sources: list[dict]) -> dict:
    """Sub-threshold stops inside the span — see `event.Pause` for why this is enrichment
    rather than detection. Re-clusters the event's own located evidence (the same
    running-centroid-plus-radius walk the geometry engines use) and reports each interior
    cluster that held `PAUSE_MIN_SECONDS`, labelled against the place book.

    Only *interior* clusters count: the first and last located fixes are the event's own
    bounds (a journey is settled fix -> settled fix by construction), so a cluster touching
    either is the endpoint itself, not a stop along the way. No pauses -> no fragment.
    """
    located = []
    for s in sources:
        msg = s.get("message") or {}
        lat, lon = msg.get("lat"), msg.get("lon")
        if lat is None or lon is None:
            continue
        try:
            located.append((int(msg.get("timestamp", 0)), float(lat), float(lon)))
        except (TypeError, ValueError):
            continue
    if len(located) < 3:
        return {}
    located.sort(key=lambda f: f[0])

    clusters, current = [], None
    for ts, lat, lon in located:
        if current is not None and haversine_m(current["clat"], current["clon"], lat, lon) <= PAUSE_RADIUS_M:
            n = current["n"] + 1
            current["clat"] += (lat - current["clat"]) / n
            current["clon"] += (lon - current["clon"]) / n
            current["n"], current["last_ts"] = n, ts
            current["fixes"].append((lat, lon))
        else:
            if current is not None:
                clusters.append(current)
            current = {"clat": lat, "clon": lon, "n": 1, "first_ts": ts, "last_ts": ts,
                       "fixes": [(lat, lon)]}
    clusters.append(current)

    pauses = []
    for c in clusters[1:-1]:                           # interior only — endpoints are the bounds
        if c["last_ts"] - c["first_ts"] < PAUSE_MIN_SECONDS:
            continue
        spread = max((haversine_m(c["clat"], c["clon"], la, lo) for la, lo in c["fixes"]), default=0.0)
        pauses.append(Pause(started_at=c["first_ts"], ended_at=c["last_ts"],
                            place=_place_at(c["clat"], c["clon"], spread)))
    return {"pauses": pauses} if pauses else {}


@register_capability(Capability.SUPPORT)
def _support(sources: list[dict]) -> dict:
    """How the claim is backed: the independent evidence kinds, and a grade over their count.

    Kinds are read off the evidence **structurally**, mirroring `_vehicle`: located sources are
    the `geometry` kind (the movement itself), and each *derived* source — an upstream claim
    that contributed as evidence (ADR 0011) — is a kind named by its event name. Framework code
    never learns what a claim is called; a transit or bicycle detector becomes a kind the day a
    definition feeds it in.

    **A claim contained in another claim's evidence is not independent.** With recursion
    carrying sources, a `car_trip` arrives with its own `got_into`/`got_out` inside its sidecar;
    counting all three as kinds would let one physical detector lane vote three times — the
    same one-event-counted-once reasoning as `_vehicle`'s name dedup. So any derived source
    whose id appears in another source's sidecar is collapsed into its container.

    No kinds → no fragment: declaring `support` on an event whose evidence is all raw and
    non-located is visibly a no-op, not a fabricated grade (the `_place` precedent).
    """
    located = any(
        (s.get("message") or {}).get("lat") is not None
        and (s.get("message") or {}).get("lon") is not None
        for s in sources
    )
    contained = {
        (sub.get("message") or {}).get("id")
        for s in sources
        for sub in (s.get("sources") or [])
    }
    claims: dict[str, int] = {}
    for s in sources:
        msg = s.get("message") or {}
        name = msg.get("name")
        if (msg.get("inference_type") is None or msg.get("id") in contained
                or not isinstance(name, str)):
            continue
        ts = msg.get("timestamp", 0)
        if name not in claims or ts < claims[name]:
            claims[name] = ts
    kinds = (["geometry"] if located else []) + sorted(claims, key=lambda n: (claims[n], n))
    if not kinds:
        return {}
    return {"support": Support(
        level="corroborated" if len(kinds) >= 2 else "single_source",
        evidence_kinds=kinds,
    )}


def _mode(messages: list[dict]) -> str | None:
    """The majority *moving* motion classification across the evidence.

    Read from the stream's own `motion` array rather than inferred from speed — the phone
    already ran that classifier. `stationary` is excluded: every journey contains stopped
    fixes (its two endpoints are settled by construction), so counting them would let a long
    traffic jam relabel a drive. Ties break on the first-seen label, which is arbitrary but
    stable; None when nothing claimed a mode.
    """
    counts = Counter(
        m for msg in messages
        for m in (msg.get("motion") or [])
        if isinstance(m, str) and m != "stationary"
    )
    return counts.most_common(1)[0][0] if counts else None
