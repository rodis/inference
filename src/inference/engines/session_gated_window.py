"""Session-gated windowed end-detector.

A windowed end-detector whose sufficiency bar is *relaxed while an antecedent session
is open*. It generalises one observation: when a derived event depends on two events in
succession (a start then an end), the start is standing positive evidence for the end —
if you got into the car, at some point you'll get out. That evidence is a **latch**, not
a windowed signal, because the start and end are minutes-to-hours apart (far outside any
co-occurrence window).

The firing rule is:

    fire  ⟺  trigger present  AND  ( session_open  OR  support_score >= support_threshold )

- `trigger` (e.g. car_door_closed) is **always required** — the guardrail against
  premature closing: corroborating signals alone can never end a session.
- `gate_event` (e.g. got_into_the_car) latches "a session is in progress". While open,
  the trigger *alone* fires; the latch is **consumed on fire** (so sequential sessions
  don't reuse it) and expires after `max_open_seconds` (stale-start protection, mirroring
  session_window's max_duration).
- `support_weights` (e.g. the disconnect signals) is windowed corroboration, needed only
  when no session is open — so standalone detection stays as strict as a plain
  weighted_window.

Contrast with the other engines: weighted/decaying/bayes keep the *earliest* sighting per
contributor and never reset after firing (so they'd mis-pair sequential sessions — the
reason session_window is a separate strategy). This engine keeps the *latest* sighting
(fresh signals aren't shadowed by stale ones) and resets its window + gate on every fire.

The `got_into_the_car` gate is delivered by the runtime's in-process recursion exactly like
any other derived contributor (it's in `input_event_names()`); per-entity Quix `State`
carries the open latch across calls until the trigger closes it.
"""

from inference.engines.base import Decision, ScopedState, register_engine


@register_engine("session_gated_window")
class SessionGatedWindowEngine:
    name = "session_gated_window"   # static engine-type identity (also stamped by register_engine)

    def __init__(self, config: dict):
        self.trigger = config["trigger"]
        self.gate_event = config["gate_event"]
        # an open gate older than this is stale — treated as no session (mirrors session_window)
        self.max_open = config.get("max_open_seconds", 21600)   # 6h default
        self.window = config["window_seconds"]
        self.support_threshold = config["support_threshold"]
        self.support_weights: dict[str, float] = config.get("support_weights", {})
        self.cooldown = config.get("cooldown_seconds", 1800)

    def input_event_names(self) -> set[str]:
        return {self.trigger, self.gate_event} | set(self.support_weights)

    def decide(self, event: dict, state: ScopedState) -> Decision | None:
        msg = event.get("message") or {}
        name = msg.get("name")
        now = int(msg.get("timestamp", 0))

        if name == self.gate_event:
            # latch the open session (keep the latest start; a re-entry before we fire
            # overwrites the previous open, matching session_window). Full body stashed
            # for symmetry, though the gate is contextual — it is not carried as lineage.
            state.set("open", {"ts": now, "event": event})
            return None

        if name != self.trigger and name not in self.support_weights:
            return None                                # not a contributor (defensive; router pre-filters)

        # windowed presence for the trigger + support signals, KEEP-LATEST (a fresh signal
        # refreshes a stale one, unlike the earliest-wins windowed engines), pruned to window.
        window = {k: v for k, v in state.get("window", {}).items() if now - v["ts"] <= self.window}
        if name not in window or now >= window[name]["ts"]:
            window[name] = {"ts": now, "event": event}
        state.set("window", window)

        if self.trigger not in window:                 # trigger is necessary — never fire without it
            return None

        open_ = state.get("open")
        open_valid = open_ is not None and now - open_["ts"] <= self.max_open
        if open_ is not None and not open_valid:
            state.set("open", None)                     # drop a stale gate so it can't validate a later trigger

        support = sum(self.support_weights.get(k, 0) for k in window if k != self.trigger)
        if not (open_valid or support >= self.support_threshold):
            return None                                 # in a session, or corroborated — else hold
        if now - state.get("last_fired", 0) < self.cooldown:
            return None
        state.set("last_fired", now)
        state.set("open", None)                         # consume the session
        state.set("window", {})                         # reset for the next session

        # lineage = the trigger + present support signals (the signals the exit was detected
        # from); the gate is contextual evidence, not lineage — the start→end relationship is
        # captured downstream by the session_window that pairs this event with its start.
        contribs = [v for k, v in window.items() if k == self.trigger or k in self.support_weights]
        occurred_at = max(v["ts"] for v in contribs)
        score = 1.0 if open_valid else round(support / self.support_threshold, 3)
        return Decision(occurred_at=occurred_at, score=score, sources=tuple(v["event"] for v in contribs))
