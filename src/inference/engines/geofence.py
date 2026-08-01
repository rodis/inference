"""Server-side geofence engine.

Turns a raw `location_ping` stream into region enter/leave events, moving geofencing
OFF the phone (where iOS region-monitoring config is fragile — it's wiped whenever the
OwnTracks mode/endpoint changes) and onto the server, where regions are just data. The
phone drops to a dumb sensor at the bottom of the abstraction ladder (it only reports
lat/lon); "am I inside this region?" is decided here.

One definition per (region, direction): `entered_<slug>` fires on the outside->inside
edge, `left_<slug>` on inside->outside. Each keeps its own per-entity `inside` flag in
state and fires only on the transition, so a steady stream of pings inside a region
emits exactly one `entered_*`. The fired events are available to the windowed/session engines via the runtime's
in-process recursion — `location_ping` -> `entered_home` -> (any weighted_window that
lists it) — so no engine downstream changes. Nothing consumes them at present: the
home-by-car pair that did was deleted 2026-08-01 (issue #6).

Region definitions come from Neon and are expanded into these definitions in the
adapter (`inference.runtime.regions`); the engine itself only needs the geometry in its
`engine_config`, so the core stays free of any Neon/transport dependency.

Trade-off vs. native iOS geofencing (deliberate): a location *stream* is coarser than
CLRegion monitoring — entry time is approximate and a brief in-and-out can be missed —
but for dwell-based Experience events (a home arrival, a store visit) that's fine. The `max_accuracy_m`
gate drops points too imprecise to trust; there is no dwell/hysteresis yet (a known
limitation — jitter right on the boundary can still flap).
"""

from inference.engines.base import Decision, ScopedState, register_engine
from inference.geo import DEFAULT_MAX_SPEED_KMH, haversine_m, is_implausible_jump

# Kept as module-level aliases: `scripts/` and tests import these, and the geometry now lives
# in `inference.geo` because `stay_window` needs the same primitives (and must agree on them).
_haversine_m = haversine_m


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
        # ...and reported accuracy is not enough on its own: a fix can claim `acc: 5` and be
        # 700m wrong (2026-07-25), which would flip containment twice and emit two bogus
        # edges. So also reject fixes that imply impossible travel from the previous one.
        self.max_speed_kmh = float(config.get("max_speed_kmh", DEFAULT_MAX_SPEED_KMH))

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

        lat, lon = float(lat), float(lon)
        now = int(msg.get("timestamp", 0))

        # Reject a fix that could not physically follow the last accepted one (see
        # max_speed_kmh). Judged against the last ACCEPTED fix, so one bad point is skipped
        # and containment continues from the last trustworthy position.
        last = state.get("last_fix")
        if last is not None and is_implausible_jump(
            last["lat"], last["lon"], last["ts"], lat, lon, now, self.max_speed_kmh
        ):
            return None
        if last is None or now >= last["ts"]:
            state.set("last_fix", {"lat": lat, "lon": lon, "ts": now})

        inside = haversine_m(lat, lon, self.lat, self.lon) <= self.radius_m
        was_inside = bool(state.get("inside", False))
        # Write only on CHANGE. Quix `State` is RocksDB + a changelog topic, so an
        # unconditional write here costs a Kafka record per ping per region definition — and
        # a continuous stream samples every ~11s. (Honest accounting: `last_fix` above is
        # still written per accepted fix, so total writes are not reduced — the plausibility
        # reference must be FRESH or it can't catch a 700m/1s snap. What this removes is the
        # write that carried no information: `inside` is unchanged on the vast majority of pings.)
        if inside != was_inside:
            state.set("inside", inside)

        crossed_in = inside and not was_inside
        crossed_out = was_inside and not inside
        fires = crossed_in if self.direction == "enter" else crossed_out
        if not fires:
            return None
        return Decision(occurred_at=now, score=1.0, sources=(event,))
