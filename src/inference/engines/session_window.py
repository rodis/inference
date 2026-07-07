"""Session-pairing engine.

Pairs a *start* event with the next *end* event into one derived "session" event —
e.g. `got_into_the_car` + `got_out_the_car` → `car_trip`. Unlike the windowed engines
it doesn't sum/score contributors: it remembers the open start in per-entity state and
emits when the matching end arrives, carrying both as lineage. A start that never gets
an end within `max_duration_seconds` is discarded (no stale pairing).

This is a deliberately different strategy from weighted/decaying/bayes (which keep the
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
            state.set("open", None)               # close it either way (consume the start)
            if now - start["ts"] > self.max_duration:
                return None                       # stale start — don't pair across an implausible gap
            # event-time = the later of the two, keeping lineage monotonic (derived ts >= contributors)
            occurred_at = max(now, start["ts"])
            sources = (start["event"], event)     # start then end; the shaper projects lineage + interval from these
            return Decision(occurred_at=occurred_at, score=1.0, sources=sources)

        return None
