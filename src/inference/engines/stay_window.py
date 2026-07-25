"""Stay (dwell) detection from a location stream — places as *clusters*, not as fences.

The `geofence` engine answers "did I cross this circle?", which needs a pre-declared region
and a sample on each side of its boundary. That fails for small places on a real phone
stream: inside a shop you stand still, iOS stops producing fixes (Overland's min-distance
filter suppresses the rest), and a 40m circle can receive ZERO points — so no enter, hence
no leave, hence no visit. It also forces every place to be declared before it can ever be
seen, and mints two definitions per place.

This engine inverts it: group consecutive fixes that stay within `radius_m` of the running
centroid, and when the cluster breaks, emit a `stay` if it lasted at least `min_dwell_seconds`.
Sparse sampling degrades a stay's *precision*, not its existence — one fix during a 40-minute
gap in movement still says you were there. Places are then a *labelling* problem over stay
centroids (a lookup), not a routing problem (a definition per region). Measured on real
Overland history (2026-07-25): radius 60m / dwell 300s produced exactly two stays for the day
— 96.8min at a shop (47 fixes) and 27.7min at home (16 fixes, centroid 10m off) — while the
drive between them fragmented into ~35 singleton clusters, which is the shape we want.

Emission is at *departure*: a stay can only be known to have ended once a fix lands outside
it. So `occurred_at` is the LAST fix inside the cluster (the true end), not the fix that broke
it, and lineage carries every fix in the cluster.

Deliberate omissions, both to keep this a strategy rather than a policy: no POI naming (the
event is a generic `stay` carrying its centroid — matching it to a place is a downstream
concern), and no re-opening of a closed stay if you return (that's a second stay, correctly).
"""

from inference.engines.base import Decision, ScopedState, register_engine
from inference.geo import DEFAULT_MAX_SPEED_KMH, haversine_m, is_implausible_jump


@register_engine("stay_window")
class StayWindowEngine:
    name = "stay_window"   # static engine-type identity (also stamped by register_engine)

    def __init__(self, config: dict):
        # a fix within this distance of the running centroid belongs to the current stay
        self.radius_m = float(config.get("radius_m", 60))
        # a cluster shorter than this was passing through, not staying
        self.min_dwell = int(config.get("min_dwell_seconds", 300))
        # fixes vaguer than this can't place you — dropped without touching the cluster
        self.max_accuracy_m = float(config.get("max_accuracy_m", 100))
        # a cluster that has not seen a fix for this long is closed by the next fix wherever
        # it lands, so a sampling outage (iOS suspension, phone off) can't fuse two visits
        # hours apart into one implausible stay
        self.max_gap_seconds = int(config.get("max_gap_seconds", 3600))
        self.max_speed_kmh = float(config.get("max_speed_kmh", DEFAULT_MAX_SPEED_KMH))

    def input_event_names(self) -> set[str]:
        return {"location_ping"}

    def decide(self, event: dict, state: ScopedState) -> Decision | None:
        msg = event.get("message") or {}
        lat, lon = msg.get("lat"), msg.get("lon")
        if lat is None or lon is None:
            return None
        lat, lon = float(lat), float(lon)
        now = int(msg.get("timestamp", 0))

        acc = msg.get("acc")
        if acc is not None and float(acc) > self.max_accuracy_m:
            return None                                  # too vague to place — ignore entirely

        open_ = state.get("open")
        if open_ is None:
            state.set("open", self._new(lat, lon, now, event))
            return None

        # Out-of-order arrival is real: batched producers flush a queue after a delay (a fix
        # 714s late, delivered AFTER newer ones, observed 2026-07-25). Clustering is
        # sequential, so a fix older than the cluster's end can't extend it — skip it rather
        # than corrupt the centroid or reopen a settled boundary.
        if now < open_["last_ts"]:
            return None

        # A confidently-wrong fix must not drag the centroid or split a real stay.
        if is_implausible_jump(open_["last_lat"], open_["last_lon"], open_["last_ts"],
                               lat, lon, now, self.max_speed_kmh):
            return None

        gap = now - open_["last_ts"]
        inside = haversine_m(open_["clat"], open_["clon"], lat, lon) <= self.radius_m
        if inside and gap <= self.max_gap_seconds:
            n = open_["n"] + 1                           # running mean centroid, O(1) state
            open_["clat"] += (lat - open_["clat"]) / n
            open_["clon"] += (lon - open_["clon"]) / n
            open_["n"] = n
            open_["last_ts"], open_["last_lat"], open_["last_lon"] = now, lat, lon
            open_["events"].append(event)
            state.set("open", open_)
            return None

        # The cluster is over: this fix starts the next one either way.
        dwell = open_["last_ts"] - open_["first_ts"]
        state.set("open", self._new(lat, lon, now, event))
        if dwell < self.min_dwell:
            return None                                  # passing through, not a stay
        return Decision(
            occurred_at=open_["last_ts"],                # the true end: last fix INSIDE
            score=float(open_["n"]),                     # fixes supporting the stay
            sources=tuple(open_["events"]),
        )

    @staticmethod
    def _new(lat: float, lon: float, ts: int, event: dict) -> dict:
        return {"clat": lat, "clon": lon, "n": 1, "first_ts": ts,
                "last_ts": ts, "last_lat": lat, "last_lon": lon, "events": [event]}
