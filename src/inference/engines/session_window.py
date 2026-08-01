"""Session-pairing engine.

Pairs a *start* event with the next *end* event into one derived "session" event —
e.g. `got_into_the_car` + `got_out_the_car` → `car_trip`. Unlike the windowed engines
it doesn't sum/score contributors: it remembers the open start in per-entity state and
emits when the matching end arrives, carrying both as lineage. A start that never gets
an end within `max_duration_seconds` is discarded (no stale pairing).

This is a deliberately different strategy from weighted/decaying (which keep the
*earliest* sighting per contributor and never reset after firing, so they'd mis-pair
sequential trips). The runtime resolves the recursion in-process: a fired
`got_into_the_car` / `got_out_the_car` is re-routed here, and per-entity Quix `State`
carries the open start across calls until the end closes it.
"""

from inference.engines.base import Decision, ScopedState, register_engine


@register_engine("session_window")
class SessionWindowEngine:
    name = "session_window"   # static engine-type identity (also stamped by register_engine)

    def __init__(self, config: dict):
        self.start_event = config["start_event"]
        self.end_event = config["end_event"]
        # a start with no end within this many seconds is treated as stale and dropped
        self.max_duration = config.get("max_duration_seconds", 21600)   # 6h default

    def input_event_names(self) -> set[str]:
        return {self.start_event, self.end_event}

    def decide(self, event: dict, state: ScopedState) -> Decision | None:
        msg = event.get("message") or {}
        name = msg.get("name")
        now = int(msg.get("timestamp", 0))

        if name == self.start_event:
            # remember the (latest) open start; the matching end closes it. Stash the full
            # event body (not just ts/id) so it can be carried as a `source` — see Decision.sources.
            state.set("open", {"ts": now, "event": event})
            return None

        if name == self.end_event:
            start = state.get("open")
            if not start:
                return None                       # end with no known start — can't form a session
            if now <= start["ts"]:
                # TIME-INVERTED (issue #38): this end happened at or before the start, so it is
                # not an end for THIS session. A session cannot have zero or negative duration —
                # a physical fact, so this is a hard guard with nothing to tune (ADR 0009).
                #
                # Leave the start OPEN rather than consuming it: the real end is still to come.
                # Live case 2026-07-30 — the phone was offline ~2min, its Shortcuts arrived with
                # ~123s lag, and a #2 phantom got_out (event-time 11:11:06) was *processed after*
                # the real got_into (event-time 11:11:21) because the runtime works in arrival
                # order. Consuming the start there would have thrown away the real trip; instead
                # the phantom is dropped and the session stays open for the genuine exit.
                #
                # Note the displacement guardrail in ValidatedSessionWindowEngine cannot cover
                # this: a zero-length span holds no location fixes, so it falls below `min_fixes`
                # and correctly abstains. The two guards meet exactly here.
                return None
            state.set("open", None)               # close it (consume the start)
            if now - start["ts"] > self.max_duration:
                return None                       # stale start — don't pair across an implausible gap
            # event-time = the end, which the guard above proves is the later of the two —
            # keeping lineage monotonic (derived ts >= every contributor).
            sources = (start["event"], event)     # start then end; the shaper projects lineage + interval from these
            return Decision(occurred_at=now, score=1.0, sources=sources)

        return None
