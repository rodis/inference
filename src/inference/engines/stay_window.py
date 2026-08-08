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

Two guards added 2026-08-08 (issue #56), both measured on a 25-day replay in which HALF of all
stays were cut short against the evidence:

- **Hysteresis** (`break_fixes`): the cluster closes only after N *consecutive* fixes outside
  the radius, so a single noisy fix cannot shatter a visit. The motivating case: a 47m-accuracy
  fix landing 61.5m from the centroid — 1.5m past the radius — split a real 2.5h bakery visit
  into 5min + an 18min hole + 131min. An inside fix discards the buffered outliers as noise;
  a confirmed break replays them as the seed of the next cluster, so a genuine departure
  loses nothing.
- **Bounded resume** (`rejoin_max_hole_seconds`): a closed stay is held (not emitted) and
  resumes if the stream comes back *inside the same cluster* within the hole limit — provided
  the hole was genuinely dark (at most a noise burst observed since the close; a *tracked*
  excursion that passes back through the cluster is a second visit, not a resume) and no
  intervening fix beyond `departure_distance_m` proved a real departure. This is the positive
  half of the rule claim_fusion's geometry veto states negatively: evidence may fill a stretch
  the location stream cannot contradict, and ONLY such a stretch — a fix elsewhere finalizes
  the stay at once, and fixes wandering nearby refute the resume by their existence. The stay
  the mechanism exists for: 42min drawn at a bakery, a 72min sampling blackout, the stream
  rejoining 14m from the centroid — with a card payment at the same shop sitting in the
  blackout to prove the merge right.

Deliberate omissions, to keep this a strategy rather than a policy: no POI naming (the event
is a generic `stay` carrying its centroid — matching it to a place is a downstream concern),
and no *unbounded* re-opening — a return past the hole limit, or after a provable departure,
is a second stay, correctly (the leave-and-return ambiguity `max_gap_seconds` always guarded
against; the resume only bridges holes the stream itself left dark).
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
        # consecutive out-of-radius fixes required to close the cluster (1 = a single fix
        # breaks it, the pre-#56 behavior)
        self.break_fixes = int(config.get("break_fixes", 1))
        # a closed stay may resume if the stream rejoins its cluster within this hole
        # (0 disables: a closed stay never reopens)
        self.rejoin_max_hole = int(config.get("rejoin_max_hole_seconds", 0))
        # ...but a fix this far from its centroid proves a real departure and finalizes it
        self.departure_m = float(config.get("departure_distance_m", 500))

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

        # A held (soft-closed) stay first: this fix finalizes it, resumes it, or leaves it held.
        finalized = None
        pending = state.get("pending")
        if pending is not None:
            hole = now - pending["last_ts"]
            d_held = haversine_m(pending["clat"], pending["clon"], lat, lon)
            if hole > self.rejoin_max_hole or d_held > self.departure_m:
                # past the hole limit, or provably departed: the stay is final
                state.set("pending", None)
                finalized = self._decision(pending)
            elif d_held <= self.radius_m and pending.get("obs", 0) <= self.break_fixes:
                # The stream came back inside after a hole it left DARK: resume the stay.
                # (`obs` counts accepted fixes since the hold — more than a noise burst means
                # the stream WATCHED the entity be elsewhere, so it can contradict continuity
                # and a return is a second visit: on 2026-07-27 a tracked 29min walk passed
                # back within 60m of a held bakery stay mid-route, and resuming there dragged
                # the stay 15min into the walk's own trip.) Whatever transit noise sits in
                # the active cluster belongs to the movement, not to any stay — drop it.
                self._fold(pending, lat, lon, now, event)
                pending.pop("obs", None)
                state.set("open", pending)
                state.set("pending", None)
                return None
            else:
                # hovering nearby (the shop 100m over), or back after a tracked excursion —
                # hold the stay, count the observation, keep clustering
                pending["obs"] = pending.get("obs", 0) + 1
                state.set("pending", pending)

        # `finalized` and `_advance`'s result are mutually exclusive: _advance only ever emits
        # the held stay, and the branch above just cleared it.
        return self._advance(open_, lat, lon, now, event, state) or finalized

    def _advance(self, open_: dict, lat: float, lon: float, now: int, event: dict,
                 state: ScopedState) -> Decision | None:
        """Fold the fix into the active cluster, or close the cluster it breaks."""
        gap = now - open_["last_ts"]
        inside = haversine_m(open_["clat"], open_["clon"], lat, lon) <= self.radius_m

        if inside and gap <= self.max_gap_seconds:
            self._fold(open_, lat, lon, now, event)
            open_["out"] = []                            # noise, not departure: forgive the outliers
            decision = None
            pending = state.get("pending")
            if pending is not None and open_["last_ts"] - open_["first_ts"] >= self.min_dwell:
                # a second stay elsewhere is now inevitable, so the held one is final
                state.set("pending", None)
                decision = self._decision(pending)
            state.set("open", open_)
            return decision

        if gap <= self.max_gap_seconds:
            # An out-of-radius fix is not yet a departure — noise must not break clusters
            # (#56: one 47m-accuracy fix 1.5m past the radius split a 2.5h visit). Buffer it;
            # `break_fixes` consecutive ones confirm the break — or the buffered excursion
            # persisting a full dwell, whatever its fix count: noise is momentary, and two
            # sparse fixes ten minutes away are a place you went, not jitter.
            out = open_.get("out") or []
            out.append((lat, lon, now, event))
            if len(out) < self.break_fixes and out[-1][2] - out[0][2] < self.min_dwell:
                open_["out"] = out
                state.set("open", open_)
                return None
            breakers = out
        else:
            breakers = [(lat, lon, now, event)]          # a blackout closes it wherever this lands

        # The cluster is over: the breaker fixes seed whatever comes next.
        dwell = open_["last_ts"] - open_["first_ts"]
        state.set("open", self._rebuild(breakers))
        if dwell < self.min_dwell:
            return None                                  # passing through, not a stay
        open_.pop("out", None)
        if self.rejoin_max_hole <= 0:
            return self._decision(open_)

        # Resumable mode: hold the stay instead of emitting. If an older one is still held
        # (unreachable today — a stay-worthy cluster finalizes it at the min_dwell crossing —
        # but guaranteed here), it is final: only one stay can be resumable at a time.
        prev = state.get("pending")
        decision = self._decision(prev) if prev is not None else None
        blat, blon, bts, bev = breakers[-1]
        if (
            len(breakers) == 1
            and bts - open_["last_ts"] <= self.rejoin_max_hole
            and haversine_m(open_["clat"], open_["clon"], blat, blon) <= self.radius_m
        ):
            # A blackout whose closing fix lands back inside the cluster is a resume, not a
            # new visit: the stream went dark in place (Aug 8: 72min dark, rejoined 14m off).
            self._fold(open_, blat, blon, bts, bev)
            state.set("open", open_)
            state.set("pending", None)
            return decision
        state.set("pending", open_)
        return decision

    def _rebuild(self, breakers: list[tuple]) -> dict:
        """Re-cluster the buffered breaker fixes into the new active cluster. Transit fixes
        restart it fix by fix (harmless: sub-dwell clusters never emit); consecutive fixes
        settling somewhere new accrue dwell from the first of them, so a departure that was
        buffered loses nothing."""
        (lat, lon, ts, ev), *rest = breakers
        cluster = self._new(lat, lon, ts, ev)
        for lat, lon, ts, ev in rest:
            if (
                ts - cluster["last_ts"] <= self.max_gap_seconds
                and haversine_m(cluster["clat"], cluster["clon"], lat, lon) <= self.radius_m
            ):
                self._fold(cluster, lat, lon, ts, ev)
            else:
                cluster = self._new(lat, lon, ts, ev)
        return cluster

    def _decision(self, cluster: dict) -> Decision:
        cluster.pop("out", None)
        cluster.pop("obs", None)
        return Decision(
            occurred_at=cluster["last_ts"],              # the true end: last fix INSIDE
            score=float(cluster["n"]),                   # fixes supporting the stay
            sources=tuple(cluster["events"]),
        )

    @staticmethod
    def _fold(cluster: dict, lat: float, lon: float, now: int, event: dict) -> None:
        n = cluster["n"] + 1                             # running mean centroid, O(1) state
        cluster["clat"] += (lat - cluster["clat"]) / n
        cluster["clon"] += (lon - cluster["clon"]) / n
        cluster["n"] = n
        cluster["last_ts"], cluster["last_lat"], cluster["last_lon"] = now, lat, lon
        cluster["events"].append(event)

    @staticmethod
    def _new(lat: float, lon: float, ts: int, event: dict) -> dict:
        return {"clat": lat, "clon": lon, "n": 1, "first_ts": ts,
                "last_ts": ts, "last_lat": lat, "last_lon": lon, "events": [event]}
