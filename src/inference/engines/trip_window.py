"""Trip (journey) detection from a location stream — the structural complement of `stay`.

`car_trip` can only see a journey in **your** car, because it pairs boundaries inferred from
your car's peripherals (lock / CarPlay / the BMW door). A journey in someone else's car, as a
passenger, by train or on foot is not mistuned there — it is *invisible*. Live case that
motivates this engine (2026-07-30, issue #41): a 24km drive to the vet in a borrowed car,
recorded by Overland in full (123 fixes out, 104 back, max 119km/h, bounding-box extent 13.9km
each way — 46x `car_trip`'s displacement guardrail) and bracketed by two real `stay` events,
**Home** then **ENNETSeeKLINIK für Kleintiere**. Nothing derived fired: the day's timeline had a
home→vet→home shape with a 20-minute, 24km hole where the journey should be. Over the 14 days
around it, 6 of 26 movement segments had no `car_trip`.

So this engine derives a trip from **motion**, not from peripherals, using the one stream that
is already abundant. It is the inverse of `stay_window`: that engine groups fixes that stay
*within* `radius_m` of a running centroid and emits when the cluster breaks; this one collects
fixes while the entity is *going somewhere* and emits when it settles again. ADR 0007 already
recorded the material this leaves on the floor — a 13-minute drive "fragmented into ~35
singleton clusters, which is the shape we want" for dwell. Those singletons are the trip.

**Motion is read from the stream's own classification first, geometry last.** Overland carries
iOS's `motion` array (`driving`/`walking`/`cycling`/`running`/`stationary`) on 77% of fixes and
`vel` on 87% (14 days to 2026-08-01), so the ladder is `motion` → `vel` → speed implied against
the last accepted fix. Order matters in both directions: `motion` survives a red light where
`vel` reads 0 and would end the trip early, while the implied-speed fallback covers the ~10
fixes per leg that carry neither field. `min_speed_kmh` is only consulted for the numeric
rungs, and sits above the 4-7km/h `vel` noise seen while standing at home.

**Arrival, not last movement, closes the trip** — and neither boundary is a moving fix. A trip
is bounded by the settled fixes on each side of it: the last one before departure (kept as the
anchor) and the first one after arrival. Taking the first/last *moving* fix instead would clip
both ends, and on the vet trip the origin would have landed ~600m down the road from home,
outside Home's POI radius, so the journey would have lost its origin label. `settle_seconds`
is what distinguishes arriving from stopping at a light: non-moving fixes are buffered, spliced
back into the trip if motion resumes, and only promoted to "arrived" once the entity has stayed
still that long. It sits below `stay`'s `min_dwell_seconds` so a trip closes before the stay it
leads into opens.

**Guardrails mirror the geometry engines, deliberately, and for the same reasons.** Bounding-box
**extent** rather than net displacement (`validated_session_window`: a drive that returns to its
origin is still a drive), `max_accuracy_m` to drop fixes too vague to place, and
`is_implausible_jump` because a fix reporting `acc: 5` while sitting 700m wrong is real and
would manufacture a trip out of standing still. `min_distance_m` is what keeps wandering a
car park from becoming a journey: at the vet, 15 minutes and 510m of walking path covered a
bounding box ~100m across.

Deliberate omissions, keeping this a strategy rather than a policy: it says nothing about
*mode* or about *where* the trip went — origin, destination and mode are derived from the same
evidence by the `journey` capability, so the engine only decides that a journey happened. And a
trip is never re-opened once closed; going out again is a second trip, correctly.
"""

import logging

from inference.engines.base import Decision, ScopedState, register_engine
from inference.geo import (
    DEFAULT_MAX_SPEED_KMH,
    haversine_m,
    implied_speed_kmh,
    is_implausible_jump,
)

log = logging.getLogger(__name__)

# iOS motion classifications that mean "going somewhere". `stationary` is the explicit
# negative; an empty or absent array is *no claim*, which falls through to the numeric rungs
# rather than reading as "not moving".
MOVING_MOTIONS = frozenset({"driving", "walking", "running", "cycling"})


@register_engine("trip_window")
class TripWindowEngine:
    name = "trip_window"   # static engine-type identity (also stamped by register_engine)

    def __init__(self, config: dict):
        # Speed at or above which the NUMERIC rungs call a fix moving. Only consulted when
        # `motion` makes no claim, so it does not have to model walking — it has to clear the
        # 4-7km/h `vel` noise real fixes report while the phone sits still at home.
        self.min_speed_kmh = float(config.get("min_speed_kmh", 10))
        # How long the entity must be non-moving before it counts as ARRIVED rather than
        # stopped. Below `stay`'s min_dwell_seconds, so a trip closes before the stay opens.
        self.settle_seconds = int(config.get("settle_seconds", 180))
        # Below this bounding-box extent the entity did not go anywhere — the same physical
        # bound `car_trip` uses, and the reason wandering a car park is not a journey.
        self.min_distance_m = float(config.get("min_distance_m", 500))
        self.min_duration_seconds = int(config.get("min_duration_seconds", 180))
        # Fewer moving fixes than this is not enough evidence that a journey happened. Note
        # this is the opposite polarity to `validated_session_window`'s `min_fixes`: there the
        # fixes REFUTE a session detected from other evidence, so sparse sampling abstains;
        # here they are the ONLY evidence, so sparse sampling has nothing to report.
        self.min_fixes = int(config.get("min_fixes", 4))
        # A sampling outage longer than this ends the trip where it was last seen: a trip
        # cannot be claimed across a blackout it has no evidence for.
        self.max_gap_seconds = int(config.get("max_gap_seconds", 1800))
        # A trip still open beyond this is closed at its last fix, so a `motion` array stuck
        # on `driving` cannot accumulate an unbounded run (and unbounded state with it).
        self.max_duration_seconds = int(config.get("max_duration_seconds", 21600))
        # Mirrors stay_window: a fix vaguer than this can't place you.
        self.max_accuracy_m = float(config.get("max_accuracy_m", 100))
        self.max_speed_kmh = float(config.get("max_speed_kmh", DEFAULT_MAX_SPEED_KMH))
        self.location_event = config.get("location_event", "location_ping")
        # Events that CORROBORATE a journey without defining it — e.g. the car boundaries, which
        # prove the vehicle was involved. They never open, extend or close a run: a journey is
        # detected from motion alone, and these only ride along as evidence when they fall
        # inside one (see `_fold_marks` and the `vehicle` capability). Named in config, so the
        # engine stays a strategy and the definition owns which signals count.
        self.corroborating_events = tuple(config.get("corroborating_events", ()))

    def input_event_names(self) -> set[str]:
        return {self.location_event} | set(self.corroborating_events)

    def decide(self, event: dict, state: ScopedState) -> Decision | None:
        msg = event.get("message") or {}

        if msg.get("name") in self.corroborating_events:
            self._mark(event, msg, state)
            return None                                # corroboration never fires a trip

        lat, lon = msg.get("lat"), msg.get("lon")
        if lat is None or lon is None:
            return None
        try:
            lat, lon, now = float(lat), float(lon), int(msg.get("timestamp", 0))
        except (TypeError, ValueError):
            return None

        acc = msg.get("acc")
        if acc is not None and float(acc) > self.max_accuracy_m:
            return None                              # too vague to place — ignore entirely

        run = state.get("run")
        last = run["last"] if run else state.get("settled")

        # Out-of-order arrival is real (a batched producer flushed a fix 714s late, delivered
        # after newer ones — see stay_window). Sequential accumulation cannot use it: it would
        # corrupt the running geometry and the plausibility guard has no way to judge it.
        if last is not None and now < int(last["ts"]):
            return None

        # A confidently-wrong fix must not manufacture movement, nor inflate the box.
        if last is not None and is_implausible_jump(
            last["lat"], last["lon"], last["ts"], lat, lon, now, self.max_speed_kmh
        ):
            return None

        fix = {"lat": lat, "lon": lon, "ts": now}

        # A blackout ends whatever was open, before this fix is classified against it: the
        # entity may have travelled the whole gap, but there is no evidence of it.
        if run is not None and now - int(run["last"]["ts"]) > self.max_gap_seconds:
            decision = self._close(run, state)
            state.set("settled", {**fix, "event": event})
            return decision

        moving = self._is_moving(msg, last)

        if run is None:
            if not moving:
                state.set("settled", {**fix, "event": event})
                return None
            state.set("run", self._open(fix, event, state.get("settled")))
            return None

        if moving:
            self._extend(run, fix, event)
            state.set("run", run)
            if now - int(run["first_ts"]) >= self.max_duration_seconds:
                return self._close(run, state)       # too long to still be one journey
            return None

        # Not moving: buffer it. A stop is only an arrival once it has lasted settle_seconds,
        # so a red light or a level crossing keeps the trip open and its fixes are spliced back
        # in when motion resumes (they are part of the journey; the buffer only holds the tail).
        run["still"].append({**fix, "event": event})
        settled_for = now - int(run["still"][0]["ts"])
        self._touch(run, fix)
        if settled_for < self.settle_seconds:
            state.set("run", run)
            return None
        return self._close(run, state)

    # --- motion ------------------------------------------------------------------

    def _is_moving(self, msg: dict, last: dict | None) -> bool:
        """Is this fix part of going somewhere? `motion` (the stream's own claim) beats `vel`
        beats geometry — see the module docstring for why the order is load-bearing in both
        directions. An absent claim is not a negative claim; it falls through to the next rung.
        """
        motion = msg.get("motion")
        if isinstance(motion, list) and motion:
            if any(m in MOVING_MOTIONS for m in motion):
                return True
            if "stationary" in motion:
                return False

        vel = msg.get("vel")
        if vel is not None:
            try:
                return float(vel) >= self.min_speed_kmh
            except (TypeError, ValueError):
                pass

        if last is None:
            return False                             # nothing to measure against
        lat, lon = float(msg["lat"]), float(msg["lon"])
        dist = haversine_m(last["lat"], last["lon"], lat, lon)
        dt = int(msg.get("timestamp", 0)) - int(last["ts"])
        return implied_speed_kmh(dist, dt) >= self.min_speed_kmh

    # --- the open run -------------------------------------------------------------

    def _open(self, fix: dict, event: dict, settled: dict | None) -> dict:
        """Start a trip at the last SETTLED fix where there is one — the true departure point,
        rather than wherever the phone first reported speed. A settled fix older than
        `max_gap_seconds` is not a departure point, it is a stale one, and a run with no anchor
        at all simply starts at its first moving fix (degraded, not wrong: the runtime restarts
        and rebuilds state from the changelog, so this is a real cold-start case).
        """
        anchor = settled
        if anchor is not None and fix["ts"] - int(anchor["ts"]) > self.max_gap_seconds:
            anchor = None
        origin = anchor if anchor is not None else fix
        origin_event = anchor["event"] if anchor is not None else event

        run = {
            "sources": [origin_event],
            "la0": origin["lat"], "la1": origin["lat"],
            "lo0": origin["lon"], "lo1": origin["lon"],
            "first_ts": origin["ts"],
            "n_moving": 0,
            "last": {k: origin[k] for k in ("lat", "lon", "ts")},
            "still": [],
        }
        if anchor is not None:
            self._extend(run, fix, event)            # the moving fix that opened the run
        else:
            run["n_moving"] = 1
        return run

    def _extend(self, run: dict, fix: dict, event: dict) -> None:
        """Fold one moving fix into the run, promoting any buffered stop back into it."""
        for held in run["still"]:
            run["sources"].append(held["event"])
            self._widen(run, held)
        run["still"] = []
        run["sources"].append(event)
        run["n_moving"] += 1
        self._widen(run, fix)

    @classmethod
    def _widen(cls, run: dict, fix: dict) -> None:
        run["la0"], run["la1"] = min(run["la0"], fix["lat"]), max(run["la1"], fix["lat"])
        run["lo0"], run["lo1"] = min(run["lo0"], fix["lon"]), max(run["lo1"], fix["lon"])
        cls._touch(run, fix)

    @staticmethod
    def _touch(run: dict, fix: dict) -> None:
        """Advance the run's cursor without admitting the fix to its bounding box.

        Buffered stops go through here: the gap check and the plausibility guard must both
        chain through the *latest* position seen, or a stop measures its silence from the last
        moving fix and a bad fix gets judged against a stale one. The box is a different
        question — a stop only widens it once it is known to be part of the journey (spliced
        back in) or to be its arrival.
        """
        run["last"] = {"lat": fix["lat"], "lon": fix["lon"], "ts": fix["ts"]}

    # --- corroboration ------------------------------------------------------------

    def _mark(self, event: dict, msg: dict, state: ScopedState) -> None:
        """Latch a corroborating event, whether or not a run is open.

        The latch is the point. A car-entry boundary fires when you get in, which is *before*
        the first moving fix — so before the run exists. Measured over 25 July - 1 August, the
        entry boundary led the span's first moving fix by up to 15 minutes on sparsely-sampled
        mornings. Recording only what arrives during an open run would drop the entry evidence
        on every trip, leaving `confirmed` unreachable.

        Kept bounded by `max_duration_seconds`: the same horizon beyond which a run itself is
        no longer one journey, so a mark older than that can't belong to the next one either.
        """
        try:
            ts = int(msg.get("timestamp", 0))
        except (TypeError, ValueError):
            return
        marks = [m for m in (state.get("marks") or [])
                 if ts - int(m["ts"]) <= self.max_duration_seconds]
        marks.append({"ts": ts, "event": event})
        state.set("marks", marks)

    def _fold_marks(self, state: ScopedState, first_ts: int, end_ts: int) -> list[dict]:
        """The corroborating events lying **strictly inside** the closing span.

        No tolerance window, for two independent reasons. A mark outside the span would widen
        the `interval` capability, which projects from the lineage extent — the exact corruption
        `validated_session_window` refuses its own fixes for, and on the `ended_at` side it
        would also break `occurred_at == interval.ended_at`. And containment measured *better*
        than a pad: at ±2 min a phantom exit 31 s past a borrowed-car arrival leaked in and
        claimed the vehicle, while at zero all 14 own-car journeys kept evidence and all 6
        borrowed-car ones had none.

        Consumed marks are dropped, along with anything older than the span, so a later journey
        cannot re-use this one's evidence.
        """
        marks = state.get("marks") or []
        inside = [m for m in marks if first_ts <= int(m["ts"]) <= end_ts]
        state.set("marks", [m for m in marks if int(m["ts"]) > end_ts])
        return [m["event"] for m in inside]

    # --- closing ------------------------------------------------------------------

    def _close(self, run: dict, state: ScopedState) -> Decision | None:
        """Settle the run and judge it. The destination is the FIRST settled fix after arrival
        where there is one (so the span reads departure→arrival and the destination carries the
        place you stopped at), else the last fix seen — a trip closed by a blackout or by
        `max_duration_seconds` has no arrival fix to point at.

        The state is reset either way: a rejected run is consumed, not retried. Its fixes are
        retained raws, so `scripts/rederive.py` rebuilds the trip if a bound is later found
        wrong (invariant 19).
        """
        arrival = run["still"][0] if run["still"] else None
        sources = list(run["sources"])
        if arrival is not None:
            sources.append(arrival["event"])
            self._widen(run, arrival)
            end_ts = int(arrival["ts"])
            state.set("settled", {k: arrival[k] for k in ("lat", "lon", "ts")}
                      | {"event": arrival["event"]})
        else:
            end_ts = int(run["last"]["ts"])
            state.set("settled", None)               # nowhere trustworthy to depart from next
        state.set("run", None)

        # Evidence riding along, not shaping the verdict: the guardrails below judge the journey
        # on movement alone, exactly as they do without any corroboration configured. Sorting
        # keeps the lineage chronological now that two streams contribute to it.
        sources.extend(self._fold_marks(state, int(run["first_ts"]), end_ts))
        sources.sort(key=lambda s: int((s.get("message") or {}).get("timestamp", 0)))

        duration = end_ts - int(run["first_ts"])
        extent = haversine_m(run["la0"], run["lo0"], run["la1"], run["lo1"])
        if (run["n_moving"] < self.min_fixes
                or duration < self.min_duration_seconds
                or extent < self.min_distance_m):
            log.debug(
                "no %s: %d moving fixes, %ds, extent %.0fm (need %d / %ds / %.0fm)",
                self.name, run["n_moving"], duration, extent,
                self.min_fixes, self.min_duration_seconds, self.min_distance_m,
            )
            return None
        return Decision(
            occurred_at=end_ts,                      # arrival — for a span, interval.ended_at
            score=float(run["n_moving"]),            # moving fixes supporting the trip
            sources=tuple(sources),
        )
