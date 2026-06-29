"""Naive-Bayes windowed engine (positive-only).

Treats each contributing signal seen within the window as independent evidence and
accumulates log-odds: starting from a prior, every observed signal adds `log(LR)`
(its likelihood ratio `P(signal|event)/P(signal|¬event)`). Fires when the resulting
posterior probability crosses the threshold, then holds off for a cooldown.

Unlike `weighted_window`, the emitted `score` is a calibrated **posterior
probability** (0–1), not an arbitrary sum — so downstream "confidence" means
something. This is the positive-only variant: it counts evidence that is *present*.
Modelling *absent-but-expected* signals (negative evidence, `lr_absent`) is a
deliberate next step, not included here.
"""

import math

from inference.engines.base import Decision, ScopedState, register_engine


@register_engine("naive_bayes_window")
class NaiveBayesWindowEngine:
    name = "naive_bayes_window"   # static engine-type identity (also stamped by register_engine)

    def __init__(self, config: dict):
        self.prior = config["prior"]
        self.threshold = config["threshold"]
        self.window = config["window_seconds"]
        self.cooldown = config.get("cooldown_seconds", 1800)
        # signals: {name: {"lr": likelihood_ratio}}  — LR > 1 is evidence *for* the event
        self.signals: dict[str, dict] = config.get("signals", {})

    def input_event_names(self) -> set[str]:
        return set(self.signals)

    def decide(self, event: dict, state: ScopedState) -> Decision | None:
        msg = event.get("message") or {}
        name = msg.get("name")
        if name not in self.signals:                  # not a contributor (defensive; router pre-filters)
            return None
        now = int(msg.get("timestamp", 0))

        # window: {name: {"ts": earliest_ts, "id": event_id}} — presence-based, pruned to window
        # (mirrors weighted_window so the only difference here is the *scoring*, not the bookkeeping)
        window = state.get("window", {})
        window = {k: v for k, v in window.items() if now - v["ts"] <= self.window}
        if name not in window or now < window[name]["ts"]:
            window[name] = {"ts": now, "id": msg.get("id")}
        state.set("window", window)

        # accumulate log-odds: prior + sum of log(LR) over distinct observed signals
        log_odds = math.log(self.prior / (1 - self.prior))
        for k in window:
            log_odds += math.log(self.signals.get(k, {}).get("lr", 1.0))
        posterior = 1.0 / (1.0 + math.exp(-log_odds))

        if posterior < self.threshold:
            return None
        if now - state.get("last_fired", 0) < self.cooldown:        # cooldown
            return None
        state.set("last_fired", now)

        occurred_at = max(v["ts"] for v in window.values())
        contributors = tuple(
            {"name": k, "timestamp": v["ts"], "id": v["id"]}
            for k, v in window.items()
        )
        return Decision(occurred_at=occurred_at, score=round(posterior, 3), contributors=contributors)
