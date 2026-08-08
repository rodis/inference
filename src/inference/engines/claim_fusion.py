"""Fuse detector claims into one top-level event — ADR 0011's inference layer.

The detectors each see one facet of a journey and miss the other's. `trip` (geometry) sees any
movement — a borrowed car, a train, a walk — but needs ≥ `min_fixes` location samples, so an
Overland outage blinds it entirely. `car_trip` (peripherals) survives that outage and fires
promptly, but pairs two direction-ambiguous boundaries, which is where its phantom family lives
(#2, #26, #38). Neither is a defective version of the other, and #42's retire-or-keep question
was a false choice: this engine derives the event consumers actually see from the **union** of
the two, so a journey exists when *either* detector saw it and is corroborated when both did.

Roles, named generically so the engine stays a strategy (the definition owns which events fill
them):

- **primary** (`trip`): the claim whose evidence defines the journey — its located fixes are
  what the capabilities derive span, endpoints and mode from. A primary fires the fused event
  immediately on arrival; there is nothing to wait for, because a matching secondary has
  almost always already closed (`car_trip` ends at `got_out`; `trip` only confirms
  `settle_seconds` later).
- **secondary** (`car_trip`): a corroborating claim. Latched when it arrives, folded into the
  primary it overlaps — and, when no primary ever comes (the outage case), emitted alone after
  `secondary_timeout_seconds` as a fallback event whose span is its own boundaries. The
  timeout must exceed the primary's worst lag behind the secondary (measured +21 min max), or
  one physical journey becomes two events: a premature session-only emission plus the late
  primary's.
- **tick** (`location_ping`): consumed as a clock only, so pending secondaries can expire and
  emit without waiting for the next journey. In the exact scenario the fallback exists for —
  no location stream — there are no ticks either, so an unpaired secondary may instead ride
  out on the next event of any kind this engine sees. Bounded staleness, documented, accepted.

**Ordering.** The common order is secondary-then-primary (see above). When the primary fires
first — an exit scored later than the arrival confirmation — the late secondary finds the
journey already emitted. Events are immutable, so it is **dropped against the recorded primary
span** rather than emitted: the journey exists, it is merely `single_source` where a faster
pairing would have read `corroborated`. That loss is measurable (`scripts/vehicle_eval.py`)
and bounded; the alternative — holding every primary open for a pairing window — taxes every
journey's latency to improve a label on the rare inverted one.

**Evidence handling** (this is what ADR 0011's recursion change exists for): the fused
decision's sources are the primary's *flattened* evidence — its fixes, plus whatever marks
rode along — with the matched secondary appended as a claim (its own sidecar intact, so
`_support` can collapse its constituents rather than double-count them). A session-only
emission hoists the secondary's sidecar to top level instead, so `interval` spans the
session's boundaries rather than collapsing to one timestamp. Capabilities are derived from
this union once, by the Shaper, at the top — the detectors never carry them.
"""

import logging

from inference.engines.base import Decision, ScopedState, register_engine

log = logging.getLogger(__name__)


@register_engine("claim_fusion")
class ClaimFusionEngine:
    name = "claim_fusion"   # static engine-type identity (also stamped by register_engine)

    def __init__(self, config: dict):
        self.primary_event = config["primary_event"]
        self.secondary_event = config["secondary_event"]
        # Consumed as a clock only — lets pending secondaries expire between journeys.
        self.tick_event = config.get("tick_event", "location_ping")
        # Overlap slack when pairing a secondary's span to a primary's: the two detectors date
        # their edges differently (settled fix vs boundary signal), so exact overlap is the
        # wrong bar. Mirrors the reasoning behind trip's corroboration pad.
        self.pair_pad_seconds = int(config.get("pair_pad_seconds", 300))
        # How long an unpaired secondary waits for its primary before emitting alone. Must
        # exceed the primary's worst measured lag (+21 min) or one journey emits twice.
        self.secondary_timeout_seconds = int(config.get("secondary_timeout_seconds", 1800))
        # How long an emitted primary's span is remembered, to absorb a late secondary
        # without double-emitting. Bounded state; pruned on every latch.
        self.recent_horizon_seconds = int(config.get("recent_horizon_seconds", 21600))
        # A session-only emission must span at least this. Not a new tuning knob: the primary
        # already refuses journeys under its own duration floor, so a FALLBACK claiming one is
        # incoherent — set it to the primary's `min_duration_seconds`. This is where the
        # secondary detector's sub-2-minute phantom pairs (#26) die when the sampling is too
        # sparse for the geometry veto to catch them.
        self.min_secondary_span_seconds = int(config.get("min_secondary_span_seconds", 0))

    def input_event_names(self) -> set[str]:
        return {self.primary_event, self.secondary_event, self.tick_event}

    def decide(self, event: dict, state: ScopedState) -> Decision | None:
        msg = event.get("message") or {}
        name, now = msg.get("name"), self._ts(msg)

        if name == self.primary_event:
            return self._on_primary(event, msg, now, state)
        if name == self.secondary_event:
            self._latch_secondary(event, now, state)
            return self._pop_expired(now, state)
        self._observe_tick(now, state)
        return self._pop_expired(now, state)          # a tick also moves the clock

    # --- primary: the journey, emitted immediately ---------------------------------

    def _on_primary(self, event: dict, msg: dict, now: int, state: ScopedState) -> Decision:
        sources = list(event.get("sources") or [event])
        span = self._span(event)
        pending, matched = [], None
        for p in state.get("pending") or []:
            if matched is None and self._overlaps(span, (p["lo"], p["hi"])):
                matched = p                            # first (oldest) overlapping claim wins
            else:
                pending.append(p)
        state.set("pending", pending)
        if matched is not None:
            sources.append(matched["event"])           # the claim rides along, sidecar intact

        # Remember the emitted span so a slower secondary is absorbed, not double-emitted.
        recent = [r for r in (state.get("recent") or [])
                  if now - int(r[1]) <= self.recent_horizon_seconds]
        recent.append(list(span))
        state.set("recent", recent)

        seen: set[str] = set()
        deduped = [s for s in sources
                   if not ((i := (s.get("message") or {}).get("id")) in seen or seen.add(i))]
        return Decision(occurred_at=now, score=float(len(deduped)), sources=tuple(deduped))

    # --- secondary: latch, pair late, or emit alone on expiry ----------------------

    def _observe_tick(self, now: int, state: ScopedState) -> None:
        """Record that the location stream is alive — the fact the fallback veto reads. Any
        pending claim whose span this tick falls into (pad included) had geometry running
        through it, so a late-flushed batch still refutes it before its expiry comes up."""
        state.set("last_tick", max(int(state.get("last_tick") or 0), now))
        pending = state.get("pending") or []
        marked = False
        for p in pending:
            if not p.get("ticked") and int(p["lo"]) <= now <= int(p["hi"]) + self.pair_pad_seconds:
                p["ticked"], marked = True, True
        if marked:
            state.set("pending", pending)

    def _latch_secondary(self, event: dict, now: int, state: ScopedState) -> None:
        lo, hi = self._span(event)
        for r in state.get("recent") or []:
            if self._overlaps((lo, hi), (int(r[0]), int(r[1]))):
                log.info("%s: late %s absorbed by an already-emitted journey",
                         self.name, self.secondary_event)
                return                                 # journey exists; do not emit twice
        pending = state.get("pending") or []
        # Geometry already alive when the claim arrived? Then the primary — not the fallback —
        # owns this journey, if there is one (see `_pop_expired`).
        pending.append({"ts": now, "lo": lo, "hi": hi, "event": event,
                        "ticked": int(state.get("last_tick") or 0) >= lo})
        state.set("pending", pending)

    def _pop_expired(self, now: int, state: ScopedState) -> Decision | None:
        """Emit the oldest pending secondary whose primary never came. One per call — decide()
        returns a single Decision — so simultaneous expiries drain over subsequent events.

        **The expiry clock lags one event behind.** A single tick can jump the clock past the
        timeout AND be the very event that closes the primary — a sampling gap longer than the
        primary's `max_gap_seconds` blackout-closes the trip on the same ping that would expire
        its session (observed 2026-08-05 11:02 in replay: one 35-minute gap emitted a
        session-only journey one step before the trip arrived to claim it, doubling the
        timeline). Recursion delivers the primary *after* the current event's consumers run, so
        expiry must not act on a clock the pending secondary's own primary hasn't seen yet:
        judge against the PREVIOUS event's time, and let this event only advance the clock. A
        genuine outage then needs two events past the deadline to flush — bounded staleness,
        already accepted for the fallback path.
        """
        clock = int(state.get("clock") or 0)
        state.set("clock", max(clock, now))
        pending = state.get("pending") or []
        if not pending or clock - int(pending[0]["ts"]) <= self.secondary_timeout_seconds:
            return None
        expired = pending.pop(0)
        state.set("pending", pending)
        # The fallback exists for OUTAGES: it may only claim a journey the geometry could not
        # see. If fixes were flowing through the claim's span and no primary emerged, the
        # location stream actively refutes it — the entity demonstrably wasn't going anywhere
        # the detector could measure, which is the #2/#26 phantom family (a lock+door pair at
        # home) arriving via a new door. ADR 0009's displacement veto, applied to the union:
        # replayed over 25 days it cut session-only journeys 30 -> 6, and every survivor sat in
        # the pre-25-Jul sparse-sampling era where geometry genuinely could not see.
        if expired.get("ticked"):
            log.info("%s: unpaired %s refuted by live geometry — dropped",
                     self.name, self.secondary_event)
            return None
        if int(expired["hi"]) - int(expired["lo"]) < self.min_secondary_span_seconds:
            log.info("%s: unpaired %s under the duration floor — dropped",
                     self.name, self.secondary_event)
            return None
        ev = expired["event"]
        # Hoist the claim's sidecar so `interval` spans its boundaries, not one timestamp;
        # the claim itself stays first so `_support` still sees its constituents as contained.
        sources = [ev, *(ev.get("sources") or [])]
        log.info("%s: %s expired unpaired — emitting session-only journey",
                 self.name, self.secondary_event)
        return Decision(occurred_at=int(expired["hi"]), score=float(len(sources)),
                        sources=tuple(sources))

    # --- geometry-free span reading -------------------------------------------------

    @staticmethod
    def _ts(msg: dict) -> int:
        try:
            return int(msg.get("timestamp", 0))
        except (TypeError, ValueError):
            return 0

    def _span(self, event: dict) -> tuple[int, int]:
        """The claim's span, read from its evidence: the extent of its located sources when it
        has any (the primary's fixes), else of all its sources (the secondary's boundaries),
        else its own timestamp. The same located-first preference as `_interval`, for the same
        reason — marks outside the movement must not stretch the span used for pairing."""
        msg = event.get("message") or {}
        sources = event.get("sources") or []
        located, all_ts = [], []
        for s in sources:
            m = s.get("message") or {}
            t = self._ts(m)
            all_ts.append(t)
            if m.get("lat") is not None and m.get("lon") is not None:
                located.append(t)
        ts = located or all_ts or [self._ts(msg)]
        return min(ts), max(ts)

    def _overlaps(self, a: tuple[int, int], b: tuple[int, int]) -> bool:
        pad = self.pair_pad_seconds
        return a[0] - pad <= b[1] and b[0] - pad <= a[1]
