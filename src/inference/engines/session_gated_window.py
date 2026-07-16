"""Session-gated windowed detector.

A windowed weighted detector (like `weighted_window`) plus a **latched gate**: an
antecedent session event contributes standing positive evidence while it is open. It
generalises one observation — when a derived event depends on a start then an end, the
start is standing evidence for the end (if you got into the car, at some point you'll get
out). That evidence is a *latch*, not a windowed signal, because start and end are
minutes-to-hours apart (far outside any co-occurrence window).

Scoring is `weighted_window`'s: sum the weights of the distinct windowed signals present.
The gate adds `gate_weight` to that sum while a session is open, so a single strong signal
plus an open session can cross the threshold that two signals cross on their own:

    score = Σ weights[present signals]  (+ gate_weight if a session is open)
    fire  ⟺  score >= threshold

- `gate_event` (e.g. got_into_the_car) latches "a session is in progress". The latch is
  **consumed on fire** (sequential sessions can't reuse it) and **expires after
  `max_open_seconds`** (stale-start protection, mirroring session_window's max_duration).
- `weights` are the windowed signals (the raw exit signals). Tune them so the gate only
  lets a *reliable* single signal fire: e.g. give the trustworthy signal enough weight that
  `signal + gate_weight >= threshold`, but keep ambiguous/noisy signals below it so
  `noisy + gate_weight < threshold` — otherwise a mid-session blip (a charger unplug, a
  direction-ambiguous lock change right after the gate opens) would false-fire.
- `gate_weight < threshold`, and scoring only runs when a windowed signal arrives, so the
  gate can never fire on its own — at least one real signal is always required.

Contrast with the other engines: weighted/decaying/bayes keep the *earliest* sighting per
contributor and never reset after firing (so they'd mis-pair sequential sessions — the
reason session_window is a separate strategy). This engine keeps the *latest* sighting
(fresh signals aren't shadowed by stale ones) and resets its window + gate on every fire.

The gate event is delivered by the runtime's in-process recursion exactly like any other
derived contributor (it's in `input_event_names()`); per-entity Quix `State` carries the
open latch across calls until a firing consumes it.
"""

from inference.engines.base import Decision, ScopedState, register_engine


@register_engine("session_gated_window")
class SessionGatedWindowEngine:
    name = "session_gated_window"   # static engine-type identity (also stamped by register_engine)

    def __init__(self, config: dict):
        self.gate_event = config["gate_event"]
        self.gate_weight = config["gate_weight"]
        # an open gate older than this is stale — treated as no session (mirrors session_window)
        self.max_open = config.get("max_open_seconds", 21600)   # 6h default
        self.window = config["window_seconds"]
        self.threshold = config["threshold"]
        self.weights: dict[str, float] = config.get("weights", {})
        self.cooldown = config.get("cooldown_seconds", 1800)

    def input_event_names(self) -> set[str]:
        return {self.gate_event} | set(self.weights)

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

        if name not in self.weights:                   # not a contributor (defensive; router pre-filters)
            return None

        # windowed presence, KEEP-LATEST (a fresh signal refreshes a stale one, unlike the
        # earliest-wins windowed engines), pruned to window.
        window = {k: v for k, v in state.get("window", {}).items() if now - v["ts"] <= self.window}
        if name not in window or now >= window[name]["ts"]:
            window[name] = {"ts": now, "event": event}
        state.set("window", window)

        score = sum(self.weights.get(k, 0) for k in window)
        open_ = state.get("open")
        open_valid = open_ is not None and now - open_["ts"] <= self.max_open
        if open_ is not None and not open_valid:
            state.set("open", None)                     # drop a stale gate so it can't validate a later signal
        if open_valid:
            score += self.gate_weight                   # the open session is standing evidence

        if score < self.threshold:
            return None
        if now - state.get("last_fired", 0) < self.cooldown:
            return None
        state.set("last_fired", now)
        state.set("open", None)                          # consume the session
        state.set("window", {})                          # reset for the next session

        # lineage = the windowed signals the event was detected from; the gate is contextual
        # evidence, not lineage — the start→end relationship is captured downstream by the
        # session_window that pairs this event with its start.
        contribs = list(window.values())
        occurred_at = max(v["ts"] for v in contribs)
        return Decision(occurred_at=occurred_at, score=score, sources=tuple(v["event"] for v in contribs))
