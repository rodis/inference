"""Time-decaying weighted-window engine.

Like `weighted_window`, but a contributor's weight *fades* with age inside the
window: recent co-occurrence counts more than stale evidence. Fires when the
decayed sum of distinct contributors crosses the threshold, then holds off for a
cooldown. State (the freshest sighting per contributor + the last-fired time)
lives in the per-entity scoped Quix `State`.

Why this exists: raw signals are noisy and order/spacing carries meaning the plain
weighted sum throws away. With decay, two signals only fire the inference if they
land *close in time* — a stale signal lingering in the window contributes less the
older it gets. `half_life_seconds` tunes how tight that coupling is.
"""

from inference.engines.base import Decision, ScopedState, register_engine


@register_engine("decaying_window")
class DecayingWindowEngine:
    name = "decaying_window"   # static engine-type identity (also stamped by register_engine)

    def __init__(self, config: dict):
        self.weights: dict[str, float] = config.get("weights", {})
        self.threshold = config["threshold"]
        self.window = config["window_seconds"]
        self.cooldown = config.get("cooldown_seconds", 1800)
        # seconds for a contributor's weight to halve. Smaller → tighter temporal
        # coupling (signals must arrive closer together to fire). Defaults to half
        # the window if unset.
        self.half_life = config.get("half_life_seconds", self.window / 2)

    def input_event_names(self) -> set[str]:
        return set(self.weights)

    def _decayed(self, base: float, age: float) -> float:
        # exponential decay; clamp negative age (out-of-order) to "fresh"
        if age <= 0:
            return base
        return base * 0.5 ** (age / self.half_life)

    def decide(self, event: dict, state: ScopedState) -> Decision | None:
        msg = event.get("message") or {}
        name = msg.get("name")
        if name not in self.weights:                  # not a contributor (defensive; router pre-filters)
            return None
        now = int(msg.get("timestamp", 0))

        # window: {name: {"ts": latest_ts, "id": event_id}} — keep the FRESHEST sighting
        # per contributor (decay rewards recency, unlike weighted_window's earliest), pruned to window.
        window = state.get("window", {})
        window = {k: v for k, v in window.items() if now - v["ts"] <= self.window}
        if name not in window or now > window[name]["ts"]:
            window[name] = {"ts": now, "id": msg.get("id")}
        state.set("window", window)

        # decayed score: each distinct contributor's weight faded by its age at `now`
        score = sum(self._decayed(self.weights.get(k, 0), now - v["ts"]) for k, v in window.items())
        if score < self.threshold:
            return None
        if now - state.get("last_fired", 0) < self.cooldown:        # cooldown
            return None
        state.set("last_fired", now)

        # event-time = latest contributing signal (the moment the pattern completed)
        occurred_at = max(v["ts"] for v in window.values())
        contributors = tuple(
            {"name": k, "timestamp": v["ts"], "id": v["id"]}
            for k, v in window.items()
        )
        return Decision(occurred_at=occurred_at, score=round(score, 3), contributors=contributors)
