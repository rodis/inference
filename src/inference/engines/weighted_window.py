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

        # window: {name: {"ts": earliest_ts, "event": full_event}} — dedup-earliest, pruned to
        # window. The full event body is retained (not just id/ts) so the Decision can carry it
        # as a `source` for capability derivation downstream — see Decision.sources.
        window = state.get("window", {})
        window = {k: v for k, v in window.items() if now - v["ts"] <= self.window}
        if name not in window or now < window[name]["ts"]:
            window[name] = {"ts": now, "event": event}
        state.set("window", window)

        score = sum(self.weights.get(k, 0) for k in window)
        if score < self.threshold:
            return None
        if now - state.get("last_fired", 0) < self.cooldown:        # cooldown
            return None
        state.set("last_fired", now)

        # event-time = the latest contributing signal: the moment the pattern completed
        # and the inference first became knowable. Keeps lineage monotonic (derived ts >=
        # every contributor) and anchors cooldown/window math to real time — averaging
        # stamped derived events earlier than their own triggering signal.
        occurred_at = max(v["ts"] for v in window.values())
        sources = tuple(v["event"] for v in window.values())
        return Decision(occurred_at=occurred_at, score=score, sources=sources)
