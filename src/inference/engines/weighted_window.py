"""Weighted-window engine.

Fires when distinct contributing event types seen within a time window sum (by
weight) to a threshold, then holds off for a cooldown. State (the window of
contributors + the last-fired time) lives in the per-entity scoped Quix `State`.
"""

from inference.engines.base import Decision, ScopedState, register_engine


@register_engine("weighted_window")
class WeightedWindowEngine:
    name = "weighted_window"   # static engine-type identity (also stamped by register_engine)

    def __init__(self, config: dict):
        self.weights: dict[str, float] = config.get("weights", {})
        self.threshold = config["threshold"]
        self.window = config["window_seconds"]
        self.cooldown = config.get("cooldown_seconds", 1800)

    def input_event_names(self) -> set[str]:
        return set(self.weights)

    def decide(self, event: dict, state: ScopedState) -> Decision | None:
        msg = event.get("message") or {}
        name = msg.get("name")
        if name not in self.weights:                  # not a contributor (defensive; router pre-filters)
            return None
        now = int(msg.get("timestamp", 0))

        # window: {name: {"ts": earliest_ts, "id": event_id}} — dedup-earliest, pruned to window
        window = state.get("window", {})
        window = {k: v for k, v in window.items() if now - v["ts"] <= self.window}
        if name not in window or now < window[name]["ts"]:
            window[name] = {"ts": now, "id": msg.get("id")}
        state.set("window", window)

        score = sum(self.weights.get(k, 0) for k in window)
        if score < self.threshold:
            return None
        if now - state.get("last_fired", 0) < self.cooldown:        # cooldown
            return None
        state.set("last_fired", now)

        occurred_at = sum(v["ts"] for v in window.values()) / len(window)
        contributors = tuple(
            {"name": k, "timestamp": v["ts"], "id": v["id"]}
            for k, v in window.items()
        )
        return Decision(occurred_at=occurred_at, score=score, contributors=contributors)
