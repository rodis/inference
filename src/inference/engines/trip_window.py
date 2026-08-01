"""Trip (journey) detection from a location stream — the structural complement of `stay`.

`car_trip` can only see a journey in **your** car, because it pairs boundaries inferred from
your car's peripherals (lock / CarPlay / the BMW door). A journey in someone else's car, as a
passenger, by train or on foot is not mistuned there — it is *invisible*. Live case that
motivates this engine (2026-07-30, issue #41): a 24km drive to the vet in a borrowed car,
recorded by Overland in full (123 fixes out, 104 back, max 119km/h, bounding-box extent 13.9km
each way — 46x `car_trip`'s displacement guardrail) and bracketed by two real `stay` events,
**Home** then **ENNETSeeKLINIK für Kleintiere**. Nothing derived fired: the day's timeline had a
home→vet→home shape with a 20-minute, 24km hole where the journey should be.

**A trip is the interval between two stays**, and this engine detects both ends with the same
clustering primitive `stay_window` uses: a running-mean centroid plus a radius. You are *settled*
while consecutive fixes stay within `settle_radius_m` of that centroid; a fix that escapes it
means you left, and a cluster that holds for `settle_seconds` means you arrived. So the two
geometry engines are exact complements — a trip ends precisely where the stay it leads into
begins — rather than two independent guesses that happen to interleave.

**Why displacement and not the motion label** (issue #44, and the reason this engine was rewritten
the day after it shipped). The first version asked "is this fix moving?" from a ladder of
`motion` → `vel` → implied speed, and trusted iOS's `motion` array first so a red light with
`vel` 0 could not end a trip early. Measured against `car_trip` over 25 Jul - 1 Aug, that ran
**long on all 14 comparable journeys**, by 31 s to 1259 s at the arrival end — and `car_trip`'s
own bounds are the get-in/get-out signals, which already *bracket* the driving, so a span wider
than that is not measuring the journey at all. Three real failures, one cause:

  - `motion` stays `["driving"]` with `vel` 0 for minutes after you park (2026-08-01: 08:30:14
    and 08:33:40, stationary, still labelled driving), so the run would not close;
  - `motion: ["walking"]` and noisy `vel` (14, 18 km/h standing in a car park) re-opened the
    settling buffer, absorbing the walk from the car to the door;
  - a spurious `["cycling"]` while standing at home opened a run four minutes before the drive.

Every one is a *label* contradicting the physical fact that the entity was not going anywhere.
ADR 0009 already recorded the general form of this — prefer a physical fact over weighted or
labelled evidence — so asking "did you get anywhere?" instead of "does the phone think you're
moving?" is the same correction applied to detection. `motion` still decides the journey's
**mode**, which is what it is actually good for; it no longer decides whether a journey happened.

**Both bounds are settled fixes, not moving ones.** A trip is bounded by the last fix of the
cluster it departed (the anchor) and the first fix of the cluster it arrived in. Clipping to the
first and last *displacing* fix would put the vet trip's origin ~600m down the road, outside
Home's POI radius, so the journey would lose the origin label that is the whole point of it.
Arrival is therefore only *knowable* `settle_seconds` after it happened — but it is *dated*
correctly, because the emitted end is the cluster's first fix, not the fix that confirmed it.

**Guardrails mirror the geometry engines, deliberately.** Bounding-box **extent** rather than net
displacement (`validated_session_window`: a drive that returns to its origin is still a drive),
`max_accuracy_m` to drop fixes too vague to place, and `is_implausible_jump` because a fix
reporting `acc: 5` while sitting 700m wrong is a real case and would manufacture a journey out of
standing still. `min_distance_m` keeps wandering a car park from becoming a journey.

Deliberate omissions, keeping this a strategy rather than a policy: it says nothing about *mode*
or about *where* the trip went — origin, destination and mode are derived from the same evidence
by the `journey` capability, and whether your own car was involved by `vehicle`. A closed trip is
never re-opened; going out again is a second trip, correctly.
"""

import logging

from inference.engines.base import Decision, ScopedState, register_engine
from inference.geo import DEFAULT_MAX_SPEED_KMH, haversine_m, is_implausible_jump

log = logging.getLogger(__name__)


@register_engine("trip_window")
class TripWindowEngine:
    name = "trip_window"   # static engine-type identity (also stamped by register_engine)

    def __init__(self, config: dict):
        # A fix within this of the running centroid belongs to the same cluster — the one
        # parameter that decides both "you left" and "you arrived". Mirrors stay_window's
        # `radius_m` so the two engines agree on what standing still looks like.
        self.settle_radius_m = float(config.get("settle_radius_m", 60))
        # How long a cluster must hold before it counts as ARRIVED rather than stopped at a
        # light. Set to stay_window's `min_dwell_seconds` so the complementarity is exact: below
        # this, neither a stay nor a trip-end exists; above it, both do, at the same instant.
        self.settle_seconds = int(config.get("settle_seconds", 300))
        # Below this bounding-box extent the entity did not go anywhere.
        self.min_distance_m = float(config.get("min_distance_m", 500))
        self.min_duration_seconds = int(config.get("min_duration_seconds", 180))
        # Fixes are the ONLY evidence here, so sparse sampling has nothing to report. Note this
        # is the opposite polarity to `validated_session_window`'s `min_fixes`: there the fixes
        # REFUTE a session detected from other evidence, so sparse sampling abstains and emits.
        self.min_fixes = int(config.get("min_fixes", 4))
        # A sampling outage longer than this ends the trip where it was last seen, and makes an
        # anchor too stale to depart from: a trip cannot be claimed across a blackout.
        self.max_gap_seconds = int(config.get("max_gap_seconds", 1800))
        # A run still open beyond this is closed at its last fix, bounding the retained state.
        self.max_duration_seconds = int(config.get("max_duration_seconds", 21600))
        # Mirrors stay_window: a fix vaguer than this can't place you.
        self.max_accuracy_m = float(config.get("max_accuracy_m", 100))
        self.max_speed_kmh = float(config.get("max_speed_kmh", DEFAULT_MAX_SPEED_KMH))
        self.location_event = config.get("location_event", "location_ping")
        # Events that CORROBORATE a journey without defining it — e.g. the car boundaries, which
        # prove the vehicle was involved. They never open, extend or close a run: a journey is
        # detected from displacement alone, and these only ride along as evidence when they fall
        # inside one (see `_fold_marks` and the `vehicle` capability). Named in config, so the
        # engine stays a strategy and the definition owns which signals count.
        self.corroborating_events = tuple(config.get("corroborating_events", ()))
        # How far OUTSIDE the span a corroborating event may sit and still count. Non-zero
        # because a correctly-measured journey systematically excludes both boundaries: you get
        # in before the phone leaves the departure cluster and get out after it enters the
        # arrival one. The first version of this engine measured spans far too wide (issue #44),
        # which made zero tolerance look right — the boundaries fell inside only because the span
        # was wrong. It cannot widen the `interval` capability, which is derived from the located
        # fixes alone (see `capabilities._interval`).
        self.corroboration_pad_seconds = int(config.get("corroboration_pad_seconds", 0))

    def input_event_names(self) -> set[str]:
        return {self.location_event} | set(self.corroborating_events)

    def decide(self, event: dict, state: ScopedState) -> Decision | None:
        msg = event.get("message") or {}

        if msg.get("name") in self.corroborating_events:
            self._mark(event, msg, state)
            return None                              # corroboration never fires a trip

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
        anchor = state.get("anchor")
        last = run["last"] if run else (anchor["last"] if anchor else None)

        # Out-of-order arrival is real (a batched producer flushed a fix 714s late, delivered
        # after newer ones — see stay_window). Sequential clustering cannot use it: it would
        # corrupt the running centroid and the plausibility guard has no way to judge it.
        if last is not None and now < int(last["ts"]):
            return None

        # A confidently-wrong fix must not manufacture displacement, nor inflate the box.
        if last is not None and is_implausible_jump(
            last["lat"], last["lon"], last["ts"], lat, lon, now, self.max_speed_kmh
        ):
            return None

        fix = {"lat": lat, "lon": lon, "ts": now}

        # A blackout ends whatever was open, before this fix is judged against it: the entity may
        # have travelled the whole gap, but there is no evidence of it — and an anchor from
        # before it is a stale departure point, not a departure point.
        if last is not None and now - int(last["ts"]) > self.max_gap_seconds:
            decision = self._close(run, state) if run else None
            state.set("anchor", self._cluster(fix, event))
            return decision

        if run is None:
            return self._while_settled(anchor, fix, event, state)
        return self._while_travelling(run, fix, event, state)

    # --- settled: waiting to leave ------------------------------------------------

    def _while_settled(self, anchor, fix, event, state) -> Decision | None:
        """Extend the cluster we are sitting in, or open a run when a fix escapes it.

        This is where the label-based version opened a run four minutes early on a spurious
        `["cycling"]` while the phone sat at home: the fix had a movement label but had not moved.
        Escaping a radius cannot be faked by a label.
        """
        if anchor is None:
            state.set("anchor", self._cluster(fix, event))
            return None
        if haversine_m(anchor["clat"], anchor["clon"], fix["lat"], fix["lon"]) <= self.settle_radius_m:
            self._absorb(anchor, fix, event)
            state.set("anchor", anchor)
            return None

        # Left. The journey departs from the anchor's LAST fix — a real fix with a real
        # timestamp, inside the place we were, so the origin still matches its POI.
        state.set("anchor", None)                     # consumed by the run it opens
        state.set("run", {
            "sources": [anchor["last_event"], event],
            "la0": min(anchor["last"]["lat"], fix["lat"]), "la1": max(anchor["last"]["lat"], fix["lat"]),
            "lo0": min(anchor["last"]["lon"], fix["lon"]), "lo1": max(anchor["last"]["lon"], fix["lon"]),
            "first_ts": int(anchor["last"]["ts"]),
            "n": 1,
            "last": dict(fix),
            "settling": None,
        })
        return None

    # --- travelling: waiting to arrive -------------------------------------------

    def _while_travelling(self, run, fix, event, state) -> Decision | None:
        """Accumulate the journey, while testing whether a cluster is forming under us.

        The settling candidate is simply the *trailing* cluster: mid-drive, consecutive fixes are
        far enough apart that each escapes the last candidate and splices it into the run, so no
        candidate ever matures. Slow steady movement can't mature one either — the running-mean
        centroid lags behind, and a fix eventually escapes it. That is the same property that
        stops `stay_window` fusing a 13-minute drive into a stay (ADR 0007), used from the other
        side, and it is what lets one parameter mean both "still here" and "no longer moving".
        """
        settling = run["settling"]
        if settling is not None and haversine_m(
            settling["clat"], settling["clon"], fix["lat"], fix["lon"]
        ) <= self.settle_radius_m:
            self._absorb(settling, fix, event)
            run["settling"] = settling
            run["last"] = dict(fix)
            if int(fix["ts"]) - int(settling["first_ts"]) >= self.settle_seconds:
                return self._close(run, state)       # arrived, and dated at the cluster's start
            state.set("run", run)
            return None

        # Either nothing was forming, or this fix escaped it: still travelling. Whatever the
        # candidate held belongs to the journey.
        if settling is not None:
            for held, held_event in zip(settling["fixes"], settling["events"], strict=False):
                run["sources"].append(held_event)
                run["n"] += 1
                self._widen(run, held)
        run["settling"] = self._cluster(fix, event)
        run["last"] = dict(fix)
        state.set("run", run)
        if int(fix["ts"]) - int(run["first_ts"]) >= self.max_duration_seconds:
            return self._close(run, state)           # too long to still be one journey
        return None

    # --- clusters -----------------------------------------------------------------

    @staticmethod
    def _cluster(fix: dict, event: dict) -> dict:
        """A one-fix cluster. `fixes`/`events` are retained because a candidate that turns out
        not to be an arrival must hand its fixes back to the journey."""
        return {"clat": fix["lat"], "clon": fix["lon"], "n": 1,
                "first_ts": int(fix["ts"]), "last": dict(fix), "last_event": event,
                "fixes": [dict(fix)], "events": [event]}

    @staticmethod
    def _absorb(cluster: dict, fix: dict, event: dict) -> None:
        """Fold a fix into a cluster. Running-mean centroid, so state stays O(1) in position
        (the retained fix list is what grows, and only for as long as the cluster is unresolved)."""
        n = cluster["n"] + 1
        cluster["clat"] += (fix["lat"] - cluster["clat"]) / n
        cluster["clon"] += (fix["lon"] - cluster["clon"]) / n
        cluster["n"] = n
        cluster["last"] = dict(fix)
        cluster["last_event"] = event
        cluster["fixes"].append(dict(fix))
        cluster["events"].append(event)

    @staticmethod
    def _widen(run: dict, fix: dict) -> None:
        run["la0"], run["la1"] = min(run["la0"], fix["lat"]), max(run["la1"], fix["lat"])
        run["lo0"], run["lo1"] = min(run["lo0"], fix["lon"]), max(run["lo1"], fix["lon"])

    # --- corroboration ------------------------------------------------------------

    def _mark(self, event: dict, msg: dict, state: ScopedState) -> None:
        """Latch a corroborating event, whether or not a run is open.

        The latch is the point. A car-entry boundary fires when you get in, which is *before* the
        fix that escapes the anchor, so before the run exists. Recording only what arrives during
        an open run would drop the entry evidence on every trip, leaving `confirmed` unreachable.
        Bounded by `max_duration_seconds`: the horizon beyond which a run is no longer one
        journey, so a mark older than that can't belong to the next one either.
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
        """The corroborating events lying inside the closing span, plus `corroboration_pad_seconds`.

        The pad exists because a correctly-measured journey **systematically excludes** both car
        boundaries — you get in before the phone leaves the departure cluster and get out after it
        enters the arrival one. Measured over 25 Jul - 1 Aug once the spans were right: the entry
        boundary sat a median 58 s before the start and the exit 18 s after the end, so strict
        containment found evidence on only 6 of 21 journeys where 14 were in the user's own car.

        It is safe here in a way it would not have been before, and for a specific reason: the
        `interval` capability is derived from the **located fixes alone**, so a mark outside the
        span can no longer rewrite `started_at`/`ended_at` or break
        `occurred_at == interval.ended_at`. The pad buys evidence without touching the geometry.

        Consumed marks are dropped, along with anything older than the padded span, so a later
        journey cannot re-use this one's evidence.
        """
        pad = self.corroboration_pad_seconds
        marks = state.get("marks") or []
        inside = [m for m in marks if first_ts - pad <= int(m["ts"]) <= end_ts + pad]
        state.set("marks", [m for m in marks if int(m["ts"]) > end_ts + pad])
        return [m["event"] for m in inside]

    # --- closing ------------------------------------------------------------------

    def _close(self, run: dict, state: ScopedState) -> Decision | None:
        """Settle the run and judge it.

        The arrival is the **first** fix of the cluster that matured, so the span reads
        departure→arrival even though we only learn the arrival `settle_seconds` later. A run
        closed by a blackout or by `max_duration_seconds` has no matured cluster, and ends at the
        last fix seen.

        The cluster becomes the next anchor, which is what makes the day a chain: you arrive
        somewhere, and that is where the following journey departs from. State is reset either
        way — a rejected run is consumed, not retried. Its fixes are retained raws, so
        `scripts/rederive.py` rebuilds the trip if a bound is later found wrong (invariant 19).
        """
        settling = run["settling"]
        sources = list(run["sources"])
        if settling is not None:
            arrival = settling["fixes"][0]
            sources.append(settling["events"][0])
            self._widen(run, arrival)
            end_ts = int(arrival["ts"])
            state.set("anchor", settling)             # arrive here, depart from here next
        else:
            end_ts = int(run["last"]["ts"])
            state.set("anchor", None)                 # nowhere trustworthy to depart from
        state.set("run", None)

        # Evidence riding along, not shaping the verdict: the guardrails below judge the journey
        # on displacement alone, exactly as they do with no corroboration configured. Sorting
        # keeps the lineage chronological now that two streams contribute to it.
        sources.extend(self._fold_marks(state, int(run["first_ts"]), end_ts))
        sources.sort(key=lambda s: int((s.get("message") or {}).get("timestamp", 0)))

        duration = end_ts - int(run["first_ts"])
        extent = haversine_m(run["la0"], run["lo0"], run["la1"], run["lo1"])
        if (run["n"] < self.min_fixes
                or duration < self.min_duration_seconds
                or extent < self.min_distance_m):
            log.debug(
                "no %s: %d fixes, %ds, extent %.0fm (need %d / %ds / %.0fm)",
                self.name, run["n"], duration, extent,
                self.min_fixes, self.min_duration_seconds, self.min_distance_m,
            )
            return None
        return Decision(
            occurred_at=end_ts,                       # arrival — for a span, interval.ended_at
            score=float(run["n"]),                    # fixes supporting the journey
            sources=tuple(sources),
        )
