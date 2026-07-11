"""Engine decide() logic — thresholds, cooldown, windowing, decay, pairing, and that
each engine now carries the FULL source event bodies on the Decision (not {id,ts})."""

from inference.engines.decaying_window import DecayingWindowEngine
from inference.engines.naive_bayes_window import NaiveBayesWindowEngine
from inference.engines.session_window import SessionWindowEngine
from inference.engines.weighted_window import WeightedWindowEngine


# --- weighted_window ------------------------------------------------------------

def _weighted(**over):
    cfg = {"weights": {"a": 5, "b": 5}, "threshold": 10, "window_seconds": 600, "cooldown_seconds": 600}
    cfg.update(over)
    return WeightedWindowEngine(cfg)


# Realistic event-time base: the cooldown gate is `now - last_fired < cooldown` with
# last_fired defaulting to 0, so timestamps must be far enough past epoch 0 that the FIRST
# fire clears the cooldown (as real epoch timestamps always do).
T = 1_700_000_000


def test_weighted_fires_at_threshold_with_full_sources(state, event):
    eng = _weighted(cooldown_seconds=0)
    assert eng.decide(event("a", T, id="A"), state) is None         # 5 < 10
    d = eng.decide(event("b", T + 10, id="B"), state)
    assert d is not None and d.score == 10 and d.occurred_at == T + 10
    assert {s["message"]["name"] for s in d.sources} == {"a", "b"}  # full bodies, both contributors
    assert all("message" in s for s in d.sources)


def test_weighted_cooldown_suppresses_second_fire(state, event):
    eng = _weighted()
    eng.decide(event("a", T), state)
    assert eng.decide(event("b", T + 10), state) is not None        # first fire (T >> cooldown-from-0)
    eng.decide(event("a", T + 20), state)
    assert eng.decide(event("b", T + 30), state) is None            # 20s since fire < 600s cooldown


def test_weighted_prunes_events_older_than_window(state, event):
    eng = _weighted(cooldown_seconds=0)
    eng.decide(event("a", T), state)
    assert eng.decide(event("b", T + 800), state) is None           # a pruned (800 > 600) -> only 5


# --- decaying_window ------------------------------------------------------------

def test_decaying_fires_when_signals_are_close(state, event):
    eng = DecayingWindowEngine(
        {"weights": {"a": 6, "b": 6}, "threshold": 10, "window_seconds": 600,
         "half_life_seconds": 100, "cooldown_seconds": 0})
    eng.decide(event("a", 100), state)
    assert eng.decide(event("b", 100), state) is not None           # no decay -> 12 >= 10


def test_decaying_suppresses_when_signals_far_apart(state, event):
    eng = DecayingWindowEngine(
        {"weights": {"a": 6, "b": 6}, "threshold": 10, "window_seconds": 600,
         "half_life_seconds": 50, "cooldown_seconds": 0})
    eng.decide(event("a", 100), state)
    # a is 4 half-lives stale at t=300 -> 6*0.0625 + 6 (fresh b) = 6.375 < 10
    assert eng.decide(event("b", 300), state) is None


# --- naive_bayes_window ---------------------------------------------------------

def test_bayes_posterior_crosses_threshold(state, event):
    eng = NaiveBayesWindowEngine(
        {"prior": 0.1, "threshold": 0.8, "window_seconds": 600, "cooldown_seconds": 0,
         "signals": {"a": {"lr": 20}, "b": {"lr": 20}}})
    eng.decide(event("a", 100), state)
    d = eng.decide(event("b", 110), state)
    assert d is not None and d.score >= 0.8


def test_bayes_lr_below_one_is_evidence_against(state, event):
    eng = NaiveBayesWindowEngine(
        {"prior": 0.5, "threshold": 0.9, "window_seconds": 600, "cooldown_seconds": 0,
         "signals": {"a": {"lr": 5}, "b": {"lr": 0.01}}})
    eng.decide(event("a", 100), state)
    assert eng.decide(event("b", 110), state) is None               # lr<1 pulls posterior back down


# --- session_window -------------------------------------------------------------

def test_session_pairs_start_then_end_in_order(state, event):
    eng = SessionWindowEngine({"start_event": "in", "end_event": "out", "max_duration_seconds": 3600})
    assert eng.decide(event("in", 1000, id="S"), state) is None
    d = eng.decide(event("out", 1600, id="E"), state)
    assert d is not None and d.occurred_at == 1600
    assert [s["message"]["id"] for s in d.sources] == ["S", "E"]    # start then end


def test_session_drops_stale_start(state, event):
    eng = SessionWindowEngine({"start_event": "in", "end_event": "out", "max_duration_seconds": 100})
    eng.decide(event("in", 1000), state)
    assert eng.decide(event("out", 2000), state) is None            # gap 1000 > 100


def test_session_end_without_start_does_not_fire(state, event):
    eng = SessionWindowEngine({"start_event": "in", "end_event": "out"})
    assert eng.decide(event("out", 1000), state) is None
