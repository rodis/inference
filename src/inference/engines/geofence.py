"""Server-side geofence engine.

Turns a raw `location_ping` stream into region enter/leave events, moving geofencing
OFF the phone (where iOS region-monitoring config is fragile — it's wiped whenever the
OwnTracks mode/endpoint changes) and onto the server, where regions are just data. The
phone drops to a dumb sensor at the bottom of the abstraction ladder (it only reports
lat/lon); "am I inside this region?" is decided here.

One definition per (region, direction): `entered_<slug>` fires on the outside->inside
edge, `left_<slug>` on inside->outside. Each keeps its own per-entity `inside` flag in
state and fires only on the transition, so a steady stream of pings inside a region
emits exactly one `entered_*`. The fired events feed the windowed/session engines via
the runtime's in-process recursion — e.g. `location_ping` -> `entered_home` ->
(weighted_window) `arrived_home_by_car` — so no engine downstream changes.

Region definitions come from Neon and are expanded into these definitions in the
adapter (`inference.runtime.regions`); the engine itself only needs the geometry in its
`engine_config`, so the core stays free of any Neon/transport dependency.

Trade-off vs. native iOS geofencing (deliberate): a location *stream* is coarser than
CLRegion monitoring — entry time is approximate and a brief in-and-out can be missed —
but for dwell-based Experience events (a home arrival, a store visit) that's fine. The `max_accuracy_m`
gate drops points too imprecise to trust; there is no dwell/hysteresis yet (a known
limitation — jitter right on the boundary can still flap).
"""

import math

from inference.engines.base import Decision, ScopedState, register_engine

_EARTH_RADIUS_M = 6_371_000.0


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two lat/lon points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


@register_engine("geofence")
class GeofenceEngine:
    name = "geofence"   # static engine-type identity (also stamped by register_engine)

    def __init__(self, config: dict):
        self.lat = float(config["lat"])
        self.lon = float(config["lon"])
        self.radius_m = float(config["radius_m"])
        self.direction = config["direction"]                    # "enter" | "leave"
        if self.direction not in ("enter", "leave"):
            raise ValueError(f"geofence direction must be enter|leave, got {self.direction!r}")
        # the region owner: geofences are per-user, so a point only tests against its
        # owner's regions (two users' "Home" regions are different places).
        self.owner = config.get("owner")
        # points less accurate than this can't be trusted to flip containment; default
        # to the region radius (a fix vaguer than the region tells us nothing about it).
        self.max_accuracy_m = float(config.get("max_accuracy_m", self.radius_m))

    def input_event_names(self) -> set[str]:
        return {"location_ping"}

    def decide(self, event: dict, state: ScopedState) -> Decision | None:
        msg = event.get("message") or {}
        if self.owner is not None and msg.get("user_id") != self.owner:
            return None                                         # not this region's owner
        lat, lon = msg.get("lat"), msg.get("lon")
        if lat is None or lon is None:
            return None
        acc = msg.get("acc")
        if acc is not None and float(acc) > self.max_accuracy_m:
            return None                                         # too imprecise — don't touch state

        inside = _haversine_m(float(lat), float(lon), self.lat, self.lon) <= self.radius_m
        was_inside = bool(state.get("inside", False))
        state.set("inside", inside)

        crossed_in = inside and not was_inside
        crossed_out = was_inside and not inside
        fires = crossed_in if self.direction == "enter" else crossed_out
        if not fires:
            return None
        now = int(msg.get("timestamp", 0))
        return Decision(occurred_at=now, score=1.0, sources=(event,))
