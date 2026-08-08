"""Engine decide() logic — thresholds, cooldown, windowing, decay, pairing, and that
each engine now carries the FULL source event bodies on the Decision (not {id,ts})."""


from inference.engines.claim_fusion import ClaimFusionEngine
from inference.engines.decaying_window import DecayingWindowEngine
from inference.engines.session_gated_window import SessionGatedWindowEngine
from inference.engines.session_window import SessionWindowEngine
from inference.engines.stay_window import StayWindowEngine
from inference.engines.trip_window import TripWindowEngine
from inference.engines.validated_session_window import ValidatedSessionWindowEngine
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


# --- max_age_seconds: per-contributor freshness -------------------------------

def test_max_age_evicts_a_stale_contributor_early(state, event):
    # `a` is a self-repeating signal: inside the shared 600s window but no longer fresh, so
    # it must not still be there to let `b` complete the pattern at a later boundary.
    eng = _weighted(cooldown_seconds=0, max_age_seconds={"a": 60})
    eng.decide(event("a", T), state)
    assert eng.decide(event("b", T + 120), state) is None            # a is 120s old vs its 60s cap


def test_max_age_leaves_a_fresh_contributor_alone(state, event):
    eng = _weighted(cooldown_seconds=0, max_age_seconds={"a": 60})
    eng.decide(event("a", T), state)
    assert eng.decide(event("b", T + 30), state) is not None         # within a's cap


def test_max_age_does_not_shorten_the_other_contributors(state, event):
    # the cap is per-name: `b` keeps the shared window_seconds.
    eng = _weighted(cooldown_seconds=0, max_age_seconds={"a": 60})
    eng.decide(event("b", T), state)
    assert eng.decide(event("a", T + 500), state) is not None        # b is 500s old, cap is 600s


def test_max_age_lets_a_repeat_reinstate_the_contributor(state, event):
    # after eviction a fresh sighting re-enters (keep-earliest can't resurrect the stale ts),
    # so capping freshness costs nothing when the signal really is present at the boundary.
    eng = _weighted(cooldown_seconds=0, max_age_seconds={"a": 60})
    eng.decide(event("a", T), state)
    eng.decide(event("a", T + 300), state)
    assert eng.decide(event("b", T + 310), state) is not None


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


# --- issue #38: a session cannot end before it starts ---------------------------

def test_session_rejects_a_time_inverted_end(state, event):
    """Arrival order can invert event-time: the runtime processes in arrival order, so a late
    phone signal can deliver an `out` whose event-time PRECEDES an already-processed `in`.
    Live 2026-07-30: got_out@11:11:06 processed after got_into@11:11:21, minting a 0.0-minute
    car_trip."""
    eng = SessionWindowEngine({"start_event": "in", "end_event": "out"})
    eng.decide(event("in", 2000, id="S"), state)
    assert eng.decide(event("out", 1985, id="E"), state) is None


def test_session_rejects_a_zero_length_pairing(state, event):
    eng = SessionWindowEngine({"start_event": "in", "end_event": "out"})
    eng.decide(event("in", 2000, id="S"), state)
    assert eng.decide(event("out", 2000, id="E"), state) is None


def test_session_keeps_the_start_open_after_an_inverted_end(state, event):
    """The guard must DROP THE END, not consume the start — the real end is still to come.
    Consuming it would turn a phantom exit into a lost trip."""
    eng = SessionWindowEngine({"start_event": "in", "end_event": "out"})
    eng.decide(event("in", 2000, id="S"), state)
    assert eng.decide(event("out", 1985, id="PHANTOM"), state) is None
    d = eng.decide(event("out", 2600, id="REAL"), state)                  # the genuine exit
    assert d is not None and d.occurred_at == 2600
    assert [s["message"]["id"] for s in d.sources] == ["S", "REAL"]


def test_validated_keeps_the_track_across_an_inverted_end(state, event):
    """The bounding box must survive a rejected end. Clearing it would hand the eventual real
    end an empty track, which reads as 'went nowhere' and would veto a genuine trip."""
    eng = ValidatedSessionWindowEngine(
        {"start_event": "in", "end_event": "out", "min_displacement_m": 300,
         "min_fixes": 3, "min_coverage_ratio": 0.5})
    eng.decide(event("in", T), state)
    for off, lat in ((10, 47.20), (20, 47.21), (30, 47.22)):              # ~2.2km of travel
        eng.decide(event("location_ping", T + off, lat=lat, lon=8.57, acc=10), state)
    assert eng.decide(event("out", T - 5, id="PHANTOM"), state) is None   # inverted — rejected
    assert state.get("car_trip:track") or state.get("track"), "track must survive"
    assert eng.decide(event("out", T + 40, id="REAL"), state) is not None  # displacement passes


# --- session_gated_window -------------------------------------------------------

# Mirrors got_out_the_car: carplay-disconnect (6) is the reliable single signal, lock (5)
# is ambiguous/shared, power-disconnect (5) is noisy; gate_weight 4, threshold 10.
def _gated(**over):
    cfg = {"gate_event": "in", "gate_weight": 4, "max_open_seconds": 21600,
           "window_seconds": 600, "threshold": 10,
           "weights": {"carplay_off": 6, "lock": 5, "power_off": 5}, "cooldown_seconds": 600}
    cfg.update(over)
    return SessionGatedWindowEngine(cfg)


def test_gated_reliable_signal_plus_open_session_fires(state, event):
    # got in, then only the CarPlay-disconnect reaches us (charger unplugged mid-drive) —
    # the open session lets that reliable single signal close the trip. 6 + gate 4 = 10.
    eng = _gated()
    assert eng.decide(event("in", T, id="IN"), state) is None            # opens the gate
    d = eng.decide(event("carplay_off", T + 800, id="C"), state)
    assert d is not None and d.occurred_at == T + 800 and d.score == 10
    assert {s["message"]["name"] for s in d.sources} == {"carplay_off"}  # gate is contextual, not lineage


def test_gated_two_raw_signals_fire_without_session(state, event):
    # real arrival with no open trip: any two raw signals reach 10 on their own (6 + 5).
    eng = _gated()
    eng.decide(event("carplay_off", T, id="C"), state)
    d = eng.decide(event("lock", T + 10, id="L"), state)
    assert d is not None and {s["message"]["name"] for s in d.sources} == {"carplay_off", "lock"}


def test_gated_single_signal_without_session_does_not_fire(state, event):
    eng = _gated()
    assert eng.decide(event("carplay_off", T, id="C"), state) is None    # 6 < 10, no corroboration


def test_gated_ambiguous_lock_plus_session_does_not_fire(state, event):
    # a lock-change right after entry (gate just opened) must NOT close the trip: 5 + 4 = 9.
    eng = _gated()
    eng.decide(event("in", T), state)
    assert eng.decide(event("lock", T + 5), state) is None


def test_gated_noisy_power_plus_session_does_not_fire(state, event):
    # a mid-drive charger unplug must NOT close the trip: 5 + 4 = 9.
    eng = _gated()
    eng.decide(event("in", T), state)
    assert eng.decide(event("power_off", T + 300), state) is None


def test_gated_stale_session_ignored(state, event):
    eng = _gated(max_open_seconds=100)
    eng.decide(event("in", T), state)
    assert eng.decide(event("carplay_off", T + 500, id="C"), state) is None  # gate stale -> 6 < 10


def test_gated_consumes_session_so_sequential_trips_dont_reuse_it(state, event):
    eng = _gated(cooldown_seconds=0)
    eng.decide(event("in", T), state)
    assert eng.decide(event("carplay_off", T + 100), state) is not None  # trip 1 closes (gated single)
    # a second lone CarPlay-disconnect with no new "in" must NOT fire on the consumed gate
    assert eng.decide(event("carplay_off", T + 5000), state) is None





# --- stay_window ----------------------------------------------------------------

# ~9m apart (jitter while standing still) and ~1.2km apart (a different place).
_HOME = dict(lat=47.20694, lon=8.57468)
_NEAR = dict(lat=47.20702, lon=8.57472)
_AWAY = dict(lat=47.19507, lon=8.52435)


def _stay(**over):
    cfg = {"radius_m": 60, "min_dwell_seconds": 300, "max_accuracy_m": 100}
    cfg.update(over)
    return StayWindowEngine(cfg)


def _fix(event, t, **over):
    kw = {"user_id": "rods", "acc": 10, **_HOME, **over}
    return event("location_ping", t, **kw)


def test_stay_fires_at_departure_dated_by_last_fix_inside(state, event):
    eng = _stay()
    eng.decide(_fix(event, T), state)
    eng.decide(_fix(event, T + 300, **_NEAR), state)
    eng.decide(_fix(event, T + 600), state)                            # 10 min of dwell so far
    d = eng.decide(_fix(event, T + 900, **_AWAY), state)               # leaving closes it
    assert d is not None
    assert d.occurred_at == T + 600                                    # the LAST fix inside, not the breaker
    assert d.score == 3                                                # three fixes supported it
    assert len(d.sources) == 3 and all("message" in s for s in d.sources)


def test_stay_ignores_passing_through(state, event):
    eng = _stay()
    eng.decide(_fix(event, T), state)
    assert eng.decide(_fix(event, T + 60, **_AWAY), state) is None      # only 60s < 300s dwell


def test_stay_starts_a_new_cluster_after_closing_one(state, event):
    eng = _stay()
    eng.decide(_fix(event, T), state)
    eng.decide(_fix(event, T + 600), state)
    assert eng.decide(_fix(event, T + 900, **_AWAY), state) is not None  # first stay closes
    assert eng.decide(_fix(event, T + 1500, **_AWAY), state) is None     # second cluster accruing
    d = eng.decide(_fix(event, T + 2000), state)                         # back home closes the second
    assert d is not None and d.occurred_at == T + 1500


def test_stay_gap_ends_the_stay_rather_than_fusing_two_visits(state, event):
    """An iOS suspension (10h of silence, observed 2026-07-24) must not become one long stay
    spanning both visits — even though both are at the same place."""
    eng = _stay(max_gap_seconds=3600)
    eng.decide(_fix(event, T), state)
    eng.decide(_fix(event, T + 600), state)
    d = eng.decide(_fix(event, T + 600 + 7200, **_HOME), state)          # same place, 2h later
    assert d is not None and d.occurred_at == T + 600                    # closed at the pre-gap end


def test_stay_skips_out_of_order_and_implausible_fixes(state, event):
    eng = _stay()
    eng.decide(_fix(event, T), state)
    eng.decide(_fix(event, T + 600), state)
    assert eng.decide(_fix(event, T + 300, **_NEAR), state) is None       # late arrival: ignored
    assert eng.decide(_fix(event, T + 601, acc=5, **_AWAY), state) is None  # 1.2km in 1s: bad fix
    d = eng.decide(_fix(event, T + 4000, **_AWAY), state)                 # a real move later closes it
    assert d is not None and d.occurred_at == T + 600                     # end unaffected by both
    assert len(d.sources) == 2


def test_stay_ignores_vague_fixes(state, event):
    eng = _stay()
    eng.decide(_fix(event, T), state)
    eng.decide(_fix(event, T + 600), state)
    assert eng.decide(_fix(event, T + 900, acc=500, **_AWAY), state) is None  # too vague to close it
    d = eng.decide(_fix(event, T + 1200, **_AWAY), state)
    assert d is not None and d.occurred_at == T + 600


# ~100m from _HOME (plausible GPS noise, outside radius 60) and ~150m (a distinct
# place nearby, still under departure_distance_m).
_EDGE = dict(lat=47.20784, lon=8.57468)
_NEXT_DOOR = dict(lat=47.20829, lon=8.57468)


def _stay56(**over):
    """#56 config: hysteresis + bounded resume, as events/stay.yml sets them."""
    cfg = {"break_fixes": 3, "rejoin_max_hole_seconds": 7200, "departure_distance_m": 500}
    cfg.update(over)
    return _stay(**cfg)


def test_stay_hysteresis_forgives_a_noisy_edge_fix(state, event):
    """One plausible-but-noisy fix past the radius (the 2026-08-07 shatter: acc 47m, 61.5m
    from centroid) must not split the visit."""
    eng = _stay56()
    eng.decide(_fix(event, T), state)
    eng.decide(_fix(event, T + 600), state)
    assert eng.decide(_fix(event, T + 700, **_EDGE), state) is None          # outlier: buffered
    eng.decide(_fix(event, T + 800), state)                                  # back inside: forgiven
    eng.decide(_fix(event, T + 1400), state)
    for i in range(3):                                                       # real departure: held
        assert eng.decide(_fix(event, T + 1500 + i * 10, **_AWAY), state) is None
    d = eng.decide(_fix(event, T + 1530, **_AWAY), state)                    # >500m finalizes it
    assert d is not None and d.occurred_at == T + 1400                       # ONE stay, full span
    assert d.score == 4                                                      # outlier not in lineage


def test_stay_hysteresis_still_breaks_on_consecutive_outside_fixes(state, event):
    eng = _stay56()
    eng.decide(_fix(event, T), state)
    eng.decide(_fix(event, T + 600), state)
    assert eng.decide(_fix(event, T + 700, **_AWAY), state) is None          # 1 of 3
    assert eng.decide(_fix(event, T + 710, **_AWAY), state) is None          # 2 of 3
    assert eng.decide(_fix(event, T + 720, **_AWAY), state) is None          # 3: break confirmed, held
    d = eng.decide(_fix(event, T + 730, **_AWAY), state)                     # >500m finalizes it
    assert d is not None and d.occurred_at == T + 600


def test_stay_resumes_across_a_sameplace_blackout(state, event):
    """The 2026-08-08 bakery: 72min of silence, the stream rejoining 14m from the centroid,
    a payment in the dark proving continuity. One stay spanning the hole."""
    eng = _stay56()
    eng.decide(_fix(event, T), state)
    eng.decide(_fix(event, T + 600), state)
    assert eng.decide(_fix(event, T + 600 + 4300, **_NEAR), state) is None   # gap > 3600: resume
    eng.decide(_fix(event, T + 600 + 4400), state)
    for i in range(3):
        assert eng.decide(_fix(event, T + 6000 + i * 10, **_AWAY), state) is None  # break, held
    d = eng.decide(_fix(event, T + 6030, **_AWAY), state)                    # >500m finalizes it
    assert d is not None and d.occurred_at == T + 5000                       # spans the blackout
    assert d.score == 4


def test_stay_resume_is_refused_past_the_hole_limit(state, event):
    """Beyond rejoin_max_hole 'stayed' vs 'left and came back' is unknowable: two stays."""
    eng = _stay56()
    eng.decide(_fix(event, T), state)
    eng.decide(_fix(event, T + 600), state)
    assert eng.decide(_fix(event, T + 600 + 7300, **_NEAR), state) is None   # hole > 7200: held only
    d = eng.decide(_fix(event, T + 600 + 7400), state)                       # next fix finalizes
    assert d is not None and d.occurred_at == T + 600


def test_stay_resume_is_refused_after_a_proven_departure(state, event):
    """A fix beyond departure_distance_m makes the return a SECOND visit."""
    eng = _stay56()
    eng.decide(_fix(event, T), state)
    eng.decide(_fix(event, T + 600), state)
    for i in range(3):
        assert eng.decide(_fix(event, T + 700 + i * 10, **_AWAY), state) is None  # break, held
    d = eng.decide(_fix(event, T + 740, **_AWAY), state)                     # 1.2km away: final
    assert d is not None and d.occurred_at == T + 600
    eng.decide(_fix(event, T + 900), state)                                  # back home: a new stay
    eng.decide(_fix(event, T + 1500), state)
    for i in range(3):
        assert eng.decide(_fix(event, T + 1600 + i * 10, **_AWAY), state) is None
    d2 = eng.decide(_fix(event, T + 1630, **_AWAY), state)                   # >500m finalizes it
    assert d2 is not None and d2.occurred_at == T + 1500                     # second stay, own span


def test_stay_resume_is_refused_after_a_tracked_excursion(state, event):
    """2026-07-27: a 29min tracked walk passed back within 60m of a held bakery stay and
    dragged its end 15min into the walk's own trip. The stream WATCHED the excursion — it
    can contradict continuity — so the pass-through must not resume the stay."""
    eng = _stay56()
    eng.decide(_fix(event, T), state)
    eng.decide(_fix(event, T + 600), state)
    for i in range(3):
        assert eng.decide(_fix(event, T + 700 + i * 10, **_NEXT_DOOR), state) is None  # break, held
    for i in range(8):                                                       # tracked wandering nearby
        assert eng.decide(_fix(event, T + 730 + i * 10, **_EDGE), state) is None
    assert eng.decide(_fix(event, T + 810, **_NEAR), state) is None          # passes back through: NO resume
    d = eng.decide(_fix(event, T + 1200, **_NEXT_DOOR), state)               # settles next door >= min_dwell
    d = d or eng.decide(_fix(event, T + 1210, **_NEXT_DOOR), state)
    assert d is not None and d.occurred_at == T + 600                        # ends BEFORE the walk


def test_stay_held_stay_finalizes_when_a_new_stay_settles_nearby(state, event):
    """Moving to the shop 150m over (inside departure_distance_m, outside the radius): the
    held stay is emitted as soon as the new dwell crosses min_dwell — no far fix needed."""
    eng = _stay56()
    eng.decide(_fix(event, T), state)
    eng.decide(_fix(event, T + 600), state)
    for i in range(3):
        assert eng.decide(_fix(event, T + 700 + i * 10, **_NEXT_DOOR), state) is None
    d = eng.decide(_fix(event, T + 720 + 300, **_NEXT_DOOR), state)          # next-door dwell >= 300
    assert d is not None and d.occurred_at == T + 600                        # the HELD stay emits
    for i in range(3):
        d2 = eng.decide(_fix(event, T + 1200 + i * 10, **_AWAY), state)
    assert d2 is None                                                        # next-door stay now held
    d2 = eng.decide(_fix(event, T + 1300, **_AWAY), state)                   # >500m finalizes it
    assert d2 is not None and d2.occurred_at == T + 1020


# --- validated_session_window ---------------------------------------------------

# Mirrors car_trip.yml. _HOME/_NEAR (9.4m apart) stand in for a parked phone jittering;
# _AWAY is 4025m and _FAR 1007m from _HOME, so both clear min_displacement_m 300.
_FAR = dict(lat=47.21600, lon=8.57468)


def _validated(**over):
    cfg = {"start_event": "in", "end_event": "out", "max_duration_seconds": 21600,
           "min_displacement_m": 300, "min_fixes": 3, "min_coverage_ratio": 0.5,
           "max_accuracy_m": 100}
    cfg.update(over)
    return ValidatedSessionWindowEngine(cfg)


def test_validated_consumes_the_location_stream_on_top_of_the_pair():
    eng = _validated()
    assert eng.input_event_names() == {"in", "out", "location_ping"}


def test_validated_accepts_a_trip_that_moved(state, event):
    eng = _validated()
    eng.decide(event("in", T, id="S"), state)
    eng.decide(_fix(event, T + 10), state)
    eng.decide(_fix(event, T + 300, **_AWAY), state)
    eng.decide(_fix(event, T + 590, **_AWAY), state)                  # covers 580/600 of the span
    d = eng.decide(event("out", T + 600, id="E"), state)
    assert d is not None and d.occurred_at == T + 600
    # Lineage stays the pair: folding the fixes in would rewrite the interval capability,
    # which projects the trip's span from the lineage extent.
    assert [s["message"]["id"] for s in d.sources] == ["S", "E"]


def test_validated_rejects_a_stationary_session(state, event):
    """The real 2026-07-27 phantom: got_out fired on the exit and got_into on the entry, so
    the session spanned a vet visit the phone was parked through (extent 34m over 918s)."""
    eng = _validated()
    eng.decide(event("in", T), state)
    for i, off in enumerate((10, 300, 600, 900)):
        eng.decide(_fix(event, T + off, **(_NEAR if i % 2 else _HOME)), state)
    assert eng.decide(event("out", T + 918), state) is None


def test_validated_abstains_below_min_fixes(state, event):
    """The 2026-07-24 trip had exactly one fix. Sparse GPS is absence of evidence, not
    evidence of a phantom — so the trip is emitted."""
    eng = _validated()
    eng.decide(event("in", T), state)
    eng.decide(_fix(event, T + 300), state)                           # 1 fix < min_fixes 3
    assert eng.decide(event("out", T + 600), state) is not None


def test_validated_abstains_when_fixes_do_not_cover_the_session(state, event):
    """Overland batches; a stationary burst at the start says nothing about the other 90%."""
    eng = _validated()
    eng.decide(event("in", T), state)
    for off in (10, 20, 30):
        eng.decide(_fix(event, T + off), state)                       # coverage 20/3600 << 0.5
    assert eng.decide(event("out", T + 3600), state) is not None


def test_validated_ignores_fixes_outside_a_session(state, event):
    """Fixes between trips must not accumulate, or the next session inherits a stale box."""
    eng = _validated()
    eng.decide(_fix(event, T, **_AWAY), state)                        # no open session
    eng.decide(_fix(event, T + 10, **_FAR), state)
    eng.decide(event("in", T + 20), state)
    for off in (30, 300, 600):
        eng.decide(_fix(event, T + off), state)                       # stationary, 3 fixes
    assert eng.decide(event("out", T + 620), state) is None           # the earlier spread is gone


def test_validated_vague_fix_cannot_fake_displacement(state, event):
    """Guards the ACCEPT side: a fix too vague to place must not widen the box."""
    eng = _validated()
    eng.decide(event("in", T), state)
    for off in (10, 300, 600):
        eng.decide(_fix(event, T + off), state)
    eng.decide(_fix(event, T + 610, acc=500, **_AWAY), state)         # 4km away but acc 500
    assert eng.decide(event("out", T + 620), state) is None


def test_validated_implausible_jump_cannot_fake_displacement(state, event):
    """The motivating real fix reported acc 5 while sitting 700m off — reported accuracy is
    not a safety net, so the speed guard has to catch it too."""
    eng = _validated()
    eng.decide(event("in", T), state)
    for off in (10, 300, 600):
        eng.decide(_fix(event, T + off), state)
    eng.decide(_fix(event, T + 601, acc=5, **_AWAY), state)           # 4km in 1s
    assert eng.decide(event("out", T + 620), state) is None


def test_validated_accepts_an_out_and_back_drive(state, event):
    """Extent, not net displacement: a drive that returns to its origin is still a drive."""
    eng = _validated()
    eng.decide(event("in", T), state)
    eng.decide(_fix(event, T + 10), state)
    eng.decide(_fix(event, T + 300, **_FAR), state)                   # 1007m out
    eng.decide(_fix(event, T + 590), state)                           # and back home
    assert eng.decide(event("out", T + 600), state) is not None


def test_validated_rejection_still_consumes_the_session(state, event):
    """A refused trip must not leave the start open, or the next `out` pairs to it."""
    eng = _validated()
    eng.decide(event("in", T), state)
    for off in (10, 300, 600):
        eng.decide(_fix(event, T + off), state)
    assert eng.decide(event("out", T + 620), state) is None
    assert eng.decide(event("out", T + 700), state) is None           # no start left to pair


def test_validated_track_resets_between_sessions(state, event):
    """A real trip followed by a stationary one: the second must not inherit the first's box."""
    eng = _validated()
    eng.decide(event("in", T), state)
    eng.decide(_fix(event, T + 10), state)
    eng.decide(_fix(event, T + 300, **_AWAY), state)
    eng.decide(_fix(event, T + 590, **_AWAY), state)
    assert eng.decide(event("out", T + 600), state) is not None       # moved 4km: accepted
    eng.decide(event("in", T + 1000), state)
    for off in (1010, 1300, 1600):
        eng.decide(_fix(event, T + off, **_HOME), state)
    assert eng.decide(event("out", T + 1620), state) is None          # stationary: rejected


def test_validated_stale_start_does_not_leak_its_track(state, event):
    """A start that never gets a timely end is dropped by the base engine; its fixes must go
    with it, or they would be validated against the *next* session."""
    eng = _validated(max_duration_seconds=100)
    eng.decide(event("in", T), state)
    eng.decide(_fix(event, T + 10, **_AWAY), state)
    eng.decide(_fix(event, T + 20, **_FAR), state)
    eng.decide(_fix(event, T + 30, **_AWAY), state)
    assert eng.decide(event("out", T + 500), state) is None           # stale: 500 > 100
    eng.decide(event("in", T + 1000), state)
    for off in (1010, 1300, 1600):
        eng.decide(_fix(event, T + off), state)
    assert eng.decide(event("out", T + 1620), state) is None          # judged on its OWN fixes


def test_validated_ignores_a_fix_that_predates_the_session(state, event):
    """Routing order is ARRIVAL order, not event order, so a batched producer delivers old
    fixes mid-session. Caught in replay (2026-07-19 13:20): one fix from 11:26 pushed n past
    min_fixes AND stretched coverage to 9.23, suppressing a real trip on a box that actually
    measured 2h of sitting at home."""
    eng = _validated()
    eng.decide(event("in", T), state)
    eng.decide(_fix(event, T - 7200), state)                          # arrives now, dated 2h ago
    eng.decide(_fix(event, T + 10), state)
    eng.decide(_fix(event, T + 590), state)
    # Only 2 in-session fixes remain -> abstain, not reject.
    assert eng.decide(event("out", T + 600), state) is not None


def test_validated_coverage_is_a_true_fraction_of_the_session(state, event):
    """With the clamp, f0 can't precede the session, so coverage <= 1 and the guard means
    what it says. A stale fix must not make a 20s stationary burst look like full coverage."""
    eng = _validated()
    eng.decide(event("in", T), state)
    eng.decide(_fix(event, T - 3600), state)                          # dropped
    for off in (10, 20, 30):
        eng.decide(_fix(event, T + off), state)                       # 3 fixes, coverage 20/3600
    assert eng.decide(event("out", T + 3600), state) is not None       # abstains on coverage


# --- trip_window ----------------------------------------------------------------
#
# Mirrors trip.yml. _HOME -> _AWAY is 4025m so a journey between them clears min_distance_m 500;
# _HOME -> _NEAR is 9.4m (a parked phone jittering), which must stay one cluster.
#
# Movement here is DISPLACEMENT, never the phone's motion label (issue #44) — so these fixtures
# deliberately carry motion/vel values that CONTRADICT the geometry, and assert the geometry wins.


def _trip(**over):
    cfg = {"settle_radius_m": 60, "settle_seconds": 300, "min_distance_m": 500,
           "min_duration_seconds": 120, "min_fixes": 4, "max_gap_seconds": 1800,
           "max_duration_seconds": 21600, "max_accuracy_m": 100}
    cfg.update(over)
    return TripWindowEngine(cfg)


def _between(a, b, frac):
    return dict(lat=a["lat"] + (b["lat"] - a["lat"]) * frac,
                lon=a["lon"] + (b["lon"] - a["lon"]) * frac)


def _sit(eng, state, event, t0, *, at=_HOME, n=3, step=60, **over):
    """Sit still at `at` (small jitter), returning the timestamp of the last fix."""
    for i in range(n):
        eng.decide(_fix(event, t0 + i * step, **(at if i % 2 else {**at, "lat": at["lat"] + 0.00005}), **over), state)
    return t0 + (n - 1) * step


def _leg(eng, state, event, t0, *, frm=_HOME, to=_AWAY, n=6, step=60, **over):
    """Travel frm->to in n displacing fixes, each well beyond settle_radius_m of the last.

    Deliberately stops SHORT of `to` (fractions i/(n+1)), so the last travelling fix is not
    already inside the arrival cluster — otherwise the emitted arrival is that fix rather than
    the first fix of the dwell, which is correct engine behaviour but makes the fixture ambiguous.
    """
    out = None
    for i in range(n):
        out = eng.decide(_fix(event, t0 + i * step, **_between(frm, to, (i + 1) / (n + 1)), **over), state)
    return t0 + (n - 1) * step, out


def _arrive(eng, state, event, t, *, at=_AWAY, **over):
    """Arrive at `at` and stay: the cluster must hold settle_seconds before it counts, so the
    emitted end is the FIRST fix here and the fix that CONFIRMS it comes 300s later."""
    assert eng.decide(_fix(event, t, **at, **over), state) is None
    assert eng.decide(_fix(event, t + 120, **at, **over), state) is None      # 120s < 300s
    return eng.decide(_fix(event, t + 330, **at, **over), state)


def test_trip_consumes_only_the_location_stream():
    assert _trip().input_event_names() == {"location_ping"}


def test_trip_spans_departure_to_arrival_using_the_cluster_bounds(state, event):
    """Both bounds are settled fixes. Clipping to the first/last DISPLACING fix would have put
    the 30-07 vet trip's origin ~600m down the road, outside Home's POI radius, losing the origin
    label the event exists for."""
    eng = _trip()
    dep = _sit(eng, state, event, T)                                   # the departure cluster
    last, _ = _leg(eng, state, event, T + 300)
    d = _arrive(eng, state, event, last + 60)
    assert d is not None
    assert d.occurred_at == last + 60                                  # arrival = cluster's FIRST fix
    assert d.sources[0]["message"]["timestamp"] == dep                 # departure = anchor's LAST fix
    assert d.sources[-1]["message"]["timestamp"] == last + 60          # not the confirming fix
    assert all("message" in s for s in d.sources)


# --- issue #44: displacement beats the label, in both directions ----------------

def test_a_movement_label_while_standing_still_does_not_open_a_trip(state, event):
    """The real 2026-08-01 case: a spurious `["cycling"]` at 08:09:35 while the phone sat at home
    opened a run four minutes before the drive. A label cannot escape a radius."""
    eng = _trip()
    for i in range(8):
        eng.decide(_fix(event, T + i * 30, motion=["cycling"], vel=25,
                        **(_NEAR if i % 2 else _HOME)), state)
    assert state.get("run") is None                                    # never left the cluster
    assert _arrive(eng, state, event, T + 400, at=_HOME) is None       # and nothing to emit


def test_a_sticky_driving_label_after_parking_still_closes_the_trip(state, event):
    """`motion` stays `["driving"]` with vel 0 for minutes after you park (08:30:14 and 08:33:40
    on 2026-08-01). The label-based version could not close on that; displacement can."""
    eng = _trip()
    _sit(eng, state, event, T)
    last, _ = _leg(eng, state, event, T + 300)
    d = _arrive(eng, state, event, last + 60, motion=["driving"], vel=0)
    assert d is not None and d.occurred_at == last + 60


def test_walking_around_the_arrival_does_not_extend_the_trip(state, event):
    """`motion: ["walking"]` plus noisy vel (14, 18 km/h standing in a car park) re-opened the
    settling buffer and absorbed the walk from the car to the door — 21 minutes on 08-01."""
    eng = _trip()
    _sit(eng, state, event, T)
    last, _ = _leg(eng, state, event, T + 300)
    assert eng.decide(_fix(event, last + 60, **_AWAY), state) is None
    # pottering about within the radius, labelled walking, with junk velocities
    for i in range(4):
        eng.decide(_fix(event, last + 90 + i * 30, motion=["walking"], vel=18,
                        **{**_AWAY, "lat": _AWAY["lat"] + 0.0002 * (i % 2)}), state)
    d = eng.decide(_fix(event, last + 400, **_AWAY), state)
    assert d is not None and d.occurred_at == last + 60                # dated at the ARRIVAL


def test_a_stop_shorter_than_settle_seconds_stays_inside_the_journey(state, event):
    """A red light is not an arrival, and its fixes belong to the trip."""
    eng = _trip()
    _sit(eng, state, event, T)
    mid = _between(_HOME, _AWAY, 0.4)
    _leg(eng, state, event, T + 300, to=mid, n=3)
    for i in range(3):                                                 # 120s stopped at a light
        eng.decide(_fix(event, T + 480 + i * 60, **mid), state)
    last, _ = _leg(eng, state, event, T + 700, frm=mid, n=4)
    d = _arrive(eng, state, event, last + 60)
    assert d is not None
    assert d.sources[0]["message"]["timestamp"] == T + 120             # one journey, from the start
    assert d.occurred_at == last + 60


def test_slow_steady_movement_never_matures_a_cluster(state, event):
    """The running-mean centroid lags behind steady movement, so a fix escapes it — the property
    that stops stay_window fusing a drive into a stay (ADR 0007), used from the other side."""
    eng = _trip()
    _sit(eng, state, event, T)
    # 26 fixes creeping toward _AWAY over 26 minutes: never within the radius long enough
    last = T + 300
    for i in range(26):
        last = T + 300 + i * 60
        eng.decide(_fix(event, last, **_between(_HOME, _AWAY, (i + 1) / 26)), state)
    assert state.get("run") is not None                                # still travelling
    d = _arrive(eng, state, event, last + 60)
    assert d is not None


# --- guardrails -----------------------------------------------------------------

def test_trip_ignores_wandering_that_covers_no_ground(state, event):
    """15 minutes and 510m of walking path around the vet car park covered a ~100m box."""
    eng = _trip(settle_radius_m=20)                  # tight, so the wander does leave the cluster
    _sit(eng, state, event, T, n=2)
    for i in range(6):
        eng.decide(_fix(event, T + 120 + i * 30, **(_NEAR if i % 2 else _HOME)), state)
    assert _arrive(eng, state, event, T + 400, at=_HOME) is None        # extent << min_distance_m


def test_trip_ignores_a_run_too_short_to_be_a_journey(state, event):
    eng = _trip()
    _sit(eng, state, event, T)
    last, _ = _leg(eng, state, event, T + 300, n=6, step=5)             # 4km but only 25s
    assert _arrive(eng, state, event, last + 10) is None


def test_trip_admits_a_short_leg_that_covered_real_ground(state, event):
    """The duration floor is a BACKSTOP under min_distance_m, not a second opinion on it.

    2026-08-01: a 1.36km drive from the petrol station home — 14 fixes at 25-58 km/h, bracketed by
    the Avia Neuheim and Home stays — spanned 140s and was dropped by `min_duration_seconds: 180`,
    the only failing gate. Swept over the preceding 30 days, every floor from 120 down to zero
    admitted that trip and nothing else, so below 180 the parameter was inert and extent was
    already carrying the decision (ADR 0010 addendum 3). Short and "went nowhere" are different
    claims; only extent tests the second one.
    """
    eng = _trip()
    dep = _sit(eng, state, event, T, n=2, step=30)                      # depart at T+30
    last, _ = _leg(eng, state, event, T + 60, n=5, step=30)             # 4km in 5 displacing fixes
    d = _arrive(eng, state, event, last + 20)
    assert d is not None
    span = d.occurred_at - dep
    assert 120 <= span < 180                                           # admitted now, rejected at 180


def test_trip_ignores_a_run_with_too_few_fixes(state, event):
    eng = _trip()
    _sit(eng, state, event, T)
    last, _ = _leg(eng, state, event, T + 300, n=2, step=120)           # 2 < min_fixes 4
    assert _arrive(eng, state, event, last + 60) is None


def test_trip_gap_ends_the_trip_where_it_was_last_seen(state, event):
    """A trip cannot be claimed across a blackout it has no evidence for."""
    eng = _trip(max_gap_seconds=600)
    _sit(eng, state, event, T)
    last, _ = _leg(eng, state, event, T + 300)
    d = eng.decide(_fix(event, last + 5000, **_FAR), state)             # 5000s of silence
    assert d is not None and d.occurred_at == last                     # the last fix seen


def test_a_stale_anchor_cannot_become_a_departure_point(state, event):
    """An anchor from before a blackout is a stale departure point, not a departure point."""
    eng = _trip(max_gap_seconds=600)
    _sit(eng, state, event, T, n=2)
    last, _ = _leg(eng, state, event, T + 5000)                        # anchor 5000s stale
    d = _arrive(eng, state, event, last + 60)
    assert d is not None and d.sources[0]["message"]["timestamp"] == T + 5000


def test_trip_skips_out_of_order_and_implausible_fixes(state, event):
    eng = _trip()
    _sit(eng, state, event, T)
    last, _ = _leg(eng, state, event, T + 300)
    assert eng.decide(_fix(event, T + 310, **_AWAY), state) is None     # late arrival: ignored
    assert eng.decide(_fix(event, last + 1, acc=5, **_FAR), state) is None  # 3km in 1s: bad fix
    d = _arrive(eng, state, event, last + 60)
    assert d is not None and d.occurred_at == last + 60


def test_trip_ignores_vague_fixes(state, event):
    eng = _trip()
    _sit(eng, state, event, T)
    last, _ = _leg(eng, state, event, T + 300)
    assert eng.decide(_fix(event, last + 60, acc=500, **_AWAY), state) is None  # too vague
    d = _arrive(eng, state, event, last + 120)
    assert d is not None and d.occurred_at == last + 120


def test_the_arrival_cluster_becomes_the_next_departure(state, event):
    """A day is a chain: you arrive somewhere, and that is where the next journey departs from."""
    eng = _trip()
    _sit(eng, state, event, T)
    last, _ = _leg(eng, state, event, T + 300)
    d1 = _arrive(eng, state, event, last + 60)
    assert d1 is not None
    back, _ = _leg(eng, state, event, last + 800, frm=_AWAY, to=_HOME)
    d2 = _arrive(eng, state, event, back + 60, at=_HOME)
    assert d2 is not None
    # departs from a fix inside the cluster it arrived in, not from the escaping fix
    assert d2.sources[0]["message"]["timestamp"] <= last + 390


# --- corroboration (the `vehicle` capability's evidence) -------------------------


def _corroborated(**over):
    return _trip(corroborating_events=["got_into_the_car", "got_out_the_car"], **over)


def test_trip_consumes_the_corroborating_events_too():
    assert _corroborated().input_event_names() == {
        "location_ping", "got_into_the_car", "got_out_the_car"}


def test_trip_folds_corroboration_that_falls_inside_the_span(state, event):
    eng = _corroborated()
    dep = _sit(eng, state, event, T)
    eng.decide(event("got_into_the_car", dep + 10), state)
    last, _ = _leg(eng, state, event, T + 300)
    eng.decide(event("got_out_the_car", last + 30), state)
    d = _arrive(eng, state, event, last + 60)
    assert d is not None
    names = [s["message"]["name"] for s in d.sources]
    assert names.count("got_into_the_car") == 1 and names.count("got_out_the_car") == 1
    assert [s["message"]["timestamp"] for s in d.sources] == \
        sorted(s["message"]["timestamp"] for s in d.sources)            # lineage stays chronological


def test_corroboration_never_widens_the_interval(state, event):
    """A mark outside the span would rewrite the interval capability, which projects from the
    lineage extent — and on the end side would break occurred_at == interval.ended_at."""
    eng = _corroborated()
    eng.decide(event("got_into_the_car", T - 300), state)                # before the departure
    dep = _sit(eng, state, event, T)
    last, _ = _leg(eng, state, event, T + 300)
    d = _arrive(eng, state, event, last + 60)
    assert d is not None
    ts = [s["message"]["timestamp"] for s in d.sources]
    assert min(ts) == dep and max(ts) == last + 60 == d.occurred_at      # span untouched
    assert "got_into_the_car" not in [s["message"]["name"] for s in d.sources]


def test_corroboration_beyond_the_pad_is_excluded(state, event):
    eng = _corroborated(corroboration_pad_seconds=60)
    _sit(eng, state, event, T)
    last, _ = _leg(eng, state, event, T + 300)
    eng.decide(event("got_out_the_car", last + 200), state)              # 140s past the arrival
    d = _arrive(eng, state, event, last + 60)
    assert d is not None and "got_out_the_car" not in [s["message"]["name"] for s in d.sources]


def test_corroboration_just_outside_the_span_counts(state, event):
    """A correctly-measured journey systematically excludes both boundaries — you get in before
    the phone leaves the departure cluster and get out after it enters the arrival one. Strict
    containment found evidence on 6 of 21 real journeys where 15 were own-car."""
    eng = _corroborated(corroboration_pad_seconds=60)
    dep = _sit(eng, state, event, T)
    eng.decide(event("got_into_the_car", dep - 40), state)               # 40s BEFORE the span
    last, _ = _leg(eng, state, event, T + 300)
    eng.decide(event("got_out_the_car", last + 100), state)              # 40s AFTER the arrival
    d = _arrive(eng, state, event, last + 60)
    assert d is not None
    assert {"got_into_the_car", "got_out_the_car"} <= {s["message"]["name"] for s in d.sources}
    # ...and the span itself is untouched, because interval derives from the located fixes alone.
    fixes = [s["message"]["timestamp"] for s in d.sources if s["message"].get("lat") is not None]
    assert min(fixes) == dep and max(fixes) == last + 60 == d.occurred_at


def test_corroboration_latches_before_the_run_exists(state, event):
    """The entry boundary fires when you get in — before the fix that escapes the anchor, so
    before the run opens. It led the span's first displacing fix by up to 15 min on sparsely
    sampled mornings, so a latch is required or `confirmed` is unreachable."""
    eng = _corroborated()
    dep = _sit(eng, state, event, T)
    # Processed while `run` is None — the latch's whole job — but stamped inside the span it will
    # belong to. Containment is judged on event-time, arrival order only decides whether it is
    # remembered long enough to be judged at all.
    eng.decide(event("got_into_the_car", dep + 30), state)
    assert state.get("run") is None
    last, _ = _leg(eng, state, event, T + 300)
    d = _arrive(eng, state, event, last + 60)
    assert d is not None and "got_into_the_car" in [s["message"]["name"] for s in d.sources]


def test_corroboration_never_opens_or_closes_a_run(state, event):
    eng = _corroborated()
    assert eng.decide(event("got_into_the_car", T), state) is None
    assert eng.decide(event("got_out_the_car", T + 60), state) is None
    assert state.get("run") is None


def test_corroboration_does_not_rescue_a_journey_that_went_nowhere(state, event):
    """Evidence rides along, it does not shape the verdict — the guardrails still judge on
    displacement, so a car-flavoured non-journey is still not a journey."""
    eng = _corroborated(settle_radius_m=20)
    _sit(eng, state, event, T, n=2)
    eng.decide(event("got_into_the_car", T + 90), state)
    for i in range(6):
        eng.decide(_fix(event, T + 120 + i * 30, **(_NEAR if i % 2 else _HOME)), state)
    eng.decide(event("got_out_the_car", T + 300), state)
    assert _arrive(eng, state, event, T + 400, at=_HOME) is None


def test_a_second_journey_cannot_reuse_the_first_ones_evidence(state, event):
    eng = _corroborated()
    dep = _sit(eng, state, event, T)
    eng.decide(event("got_into_the_car", dep + 10), state)
    last, _ = _leg(eng, state, event, T + 300)
    assert _arrive(eng, state, event, last + 60) is not None              # consumes the mark
    back, _ = _leg(eng, state, event, last + 800, frm=_AWAY, to=_HOME)
    d = _arrive(eng, state, event, back + 60, at=_HOME)
    assert d is not None and "got_into_the_car" not in [s["message"]["name"] for s in d.sources]


def test_stale_marks_do_not_accumulate(state, event):
    """Bounded state: a mark older than max_duration_seconds can't belong to the next journey."""
    eng = _corroborated(max_duration_seconds=600)
    eng.decide(event("got_into_the_car", T), state)
    eng.decide(event("got_out_the_car", T + 5000), state)                # prunes the first
    assert [m["ts"] for m in state.get("marks")] == [T + 5000]


# --- gap-tolerant corroboration (issue #46) --------------------------------------
#
# A fixed pad cannot fit both real failure modes at once: an Overland cold-start put a real
# got_into 196s before the span (the first fix of the day was already MOVING, so the span could
# not start earlier), and a parking-spot search put the real got_out 288s after it — while every
# pad past 60s only admitted borrowed-car phantoms. Gap tolerance accepts a mark when the
# location stream cannot contradict it belonging to this trip.


def _gap_tolerant(**over):
    return _corroborated(corroboration_pad_seconds=60,
                         corroboration_gap_tolerant=True, **over)


def test_a_boundary_in_the_cold_start_gap_counts(state, event):
    """The 2026-08-03 start edge: 12h of overnight silence, then a first fix already at 58km/h —
    the got_into fired in the gap, 196s before the span could start. No fix contradicts it."""
    eng = _gap_tolerant()
    eng.decide(_fix(event, T, **_HOME), state)                           # last fix before the night
    eng.decide(event("got_into_the_car", T + 5000), state)               # fires in the silence
    # First fix of the day is already under way (blackout resets the anchor), 200s after the mark.
    last, _ = _leg(eng, state, event, T + 5200)
    d = _arrive(eng, state, event, last + 60)
    assert d is not None
    assert "got_into_the_car" in [s["message"]["name"] for s in d.sources]
    # ...and the span still starts at the first located fix — evidence, not geometry.
    fixes = [s["message"]["timestamp"] for s in d.sources if s["message"].get("lat") is not None]
    assert min(fixes) == T + 5200


def test_the_gap_stops_at_the_previous_fix(state, event):
    """A fix BETWEEN the mark and the departure contradicts the mark belonging to this trip —
    the tolerance is read off the actual sampling, so a densely-sampled edge stays tight."""
    eng = _gap_tolerant()
    eng.decide(event("got_into_the_car", T + 30), state)                 # 90s before departure...
    dep = _sit(eng, state, event, T)                                     # ...but fixes keep coming
    last, _ = _leg(eng, state, event, dep + 60)
    d = _arrive(eng, state, event, last + 60)
    assert d is not None
    assert "got_into_the_car" not in [s["message"]["name"] for s in d.sources]


def test_a_boundary_during_the_parking_search_counts(state, event):
    """The 2026-08-03 end edge: arrival is dated at the settle cluster's first fix (correctly),
    then minutes of circling for a spot — the got_out fires in-cluster, past any sane pad."""
    eng = _gap_tolerant()
    _sit(eng, state, event, T)
    last, _ = _leg(eng, state, event, T + 300)
    assert eng.decide(_fix(event, last + 60, **_AWAY), state) is None    # arrival cluster opens
    assert eng.decide(_fix(event, last + 180, **_AWAY), state) is None
    eng.decide(event("got_out_the_car", last + 260), state)              # 200s past the span end
    d = eng.decide(_fix(event, last + 390, **_AWAY), state)              # cluster confirms
    assert d is not None and d.occurred_at == last + 60                  # end untouched
    assert "got_out_the_car" in [s["message"]["name"] for s in d.sources]


def test_a_boundary_after_the_arrival_cluster_broke_is_excluded(state, event):
    """The tolerance ends where the evidence does: once the entity moved on, a later boundary
    belongs to whatever comes next, not to this trip."""
    eng = _gap_tolerant()
    _sit(eng, state, event, T)
    last, _ = _leg(eng, state, event, T + 300)
    d = _arrive(eng, state, event, last + 60)                            # closes at last + 390
    assert d is not None
    eng.decide(event("got_out_the_car", last + 500), state)              # after the close
    assert [m["ts"] for m in state.get("marks")] == [last + 500]         # latched for the NEXT trip


def test_a_boundary_between_two_chained_trips_corroborates_both(state, event):
    """The 2026-08-01 petrol stop: legs minutes apart, one entry boundary between them. It fired
    while leg 1's arrival cluster still held AND belongs to leg 2's departure gap — consuming it
    on leg 1 (the first implementation) stripped leg 2's only evidence."""
    eng = _gap_tolerant()
    _sit(eng, state, event, T)
    last, _ = _leg(eng, state, event, T + 300)
    assert eng.decide(_fix(event, last + 60, **_AWAY), state) is None    # arrive at the pump
    eng.decide(event("got_into_the_car", last + 260), state)             # in-cluster, past end+pad
    d1 = eng.decide(_fix(event, last + 390, **_AWAY), state)
    assert d1 is not None
    assert "got_into_the_car" in [s["message"]["name"] for s in d1.sources]
    back, _ = _leg(eng, state, event, last + 420, frm=_AWAY, to=_HOME)   # straight back out
    d2 = _arrive(eng, state, event, back + 60, at=_HOME)
    assert d2 is not None
    assert "got_into_the_car" in [s["message"]["name"] for s in d2.sources]


def test_gap_tolerance_off_keeps_the_pad_semantics(state, event):
    """The flag gates everything: without it the cold-start mark stays excluded (this is the
    documented 2026-08-03 miss, reproduced)."""
    eng = _corroborated(corroboration_pad_seconds=60)
    eng.decide(_fix(event, T, **_HOME), state)
    eng.decide(event("got_into_the_car", T + 5000), state)
    last, _ = _leg(eng, state, event, T + 5200)
    d = _arrive(eng, state, event, last + 60)
    assert d is not None
    assert "got_into_the_car" not in [s["message"]["name"] for s in d.sources]


# --- claim_fusion (ADR 0011) ------------------------------------------------------
#
# The inference layer over the detectors: `journey` = union(trip, car_trip). The engine never
# looks at geometry — spans are read off each claim's evidence timestamps — so the fixtures
# build claims by hand: a primary carrying located fix sources, a secondary carrying two
# boundary envelopes in its sidecar (which is what the ADR 0011 recursion change delivers).


def _fusion(**over):
    cfg = {"primary_event": "trip", "secondary_event": "car_trip",
           "tick_event": "location_ping", "pair_pad_seconds": 300,
           "secondary_timeout_seconds": 1800}
    cfg.update(over)
    return ClaimFusionEngine(cfg)


def _primary(event, start, end, *, id="P"):
    """A trip claim: located fixes spanning start..end riding in its sidecar."""
    e = event("trip", end, id=id)
    e["message"]["inference_type"] = "trip_window"
    e["sources"] = [_fix(event, start, id=f"{id}f0"), _fix(event, end, id=f"{id}f1", **_AWAY)]
    return e


def _secondary(event, start, end, *, id="S"):
    """A car_trip claim: its boundary envelopes (derived, non-located) in its sidecar."""
    into = event("got_into_the_car", start, id=f"{id}in")
    into["message"]["inference_type"] = "weighted_window"
    out = event("got_out_the_car", end, id=f"{id}out")
    out["message"]["inference_type"] = "session_gated_window"
    e = event("car_trip", end, id=id)
    e["message"]["inference_type"] = "validated_session_window"
    e["sources"] = [into, out]
    return e


def test_fusion_pairs_a_session_with_the_overlapping_trip(state, event):
    eng = _fusion()
    assert eng.decide(_secondary(event, T - 60, T + 620), state) is None    # latched, no fire
    d = eng.decide(_primary(event, T, T + 600), state)
    assert d is not None and d.occurred_at == T + 600
    names = [s["message"]["name"] for s in d.sources]
    assert names.count("car_trip") == 1                                     # the claim rode along
    assert names.count("location_ping") == 2                                # with the evidence
    assert state.get("pending") == []                                       # and was consumed


def test_fusion_emits_a_trip_only_journey_immediately(state, event):
    d = _fusion().decide(_primary(event, T, T + 600), state)
    assert d is not None
    assert [s["message"]["name"] for s in d.sources] == ["location_ping", "location_ping"]


def test_fusion_does_not_pair_across_distant_spans(state, event):
    eng = _fusion()
    eng.decide(_secondary(event, T - 5000, T - 4000), state)                # yesterday's errand
    d = eng.decide(_primary(event, T, T + 600), state)
    assert "car_trip" not in [s["message"]["name"] for s in d.sources]
    assert len(state.get("pending")) == 1                                   # still waiting


def test_an_unpaired_session_emits_alone_after_the_timeout(state, event):
    """The Overland-outage fallback (#42's measured ~7%): no fixes, so no trip ever comes —
    the session claim IS the journey, and its span is its own boundaries. Expiry judges by the
    PREVIOUS event's clock (see `_pop_expired`), so it takes two events past the deadline."""
    eng = _fusion()
    eng.decide(_secondary(event, T, T + 600), state)
    # Ticks resume only AFTER the session (past its span + pad): the outage covered the drive
    # itself, so the geometry veto does not apply and the claim survives to its expiry.
    assert eng.decide(_fix(event, T + 1000), state) is None                 # not expired yet
    assert eng.decide(_fix(event, T + 600 + 1901), state) is None           # clock catches up
    d = eng.decide(_fix(event, T + 600 + 2000), state)                      # ...and now expires
    assert d is not None and d.occurred_at == T + 600
    names = [s["message"]["name"] for s in d.sources]
    assert names == ["car_trip", "got_into_the_car", "got_out_the_car"]     # sidecar hoisted
    assert state.get("pending") == []


def test_a_gap_that_closes_the_trip_cannot_also_expire_its_session(state, event):
    """The 2026-08-05 11:02 replay double: a 35-minute sampling gap blackout-closes the trip
    on the SAME ping that would expire its pending session, and the tick runs before the
    recursed trip. The lagged clock means the session survives that tick and the trip, arriving
    next, pairs with it — one journey, not two."""
    eng = _fusion()
    eng.decide(_secondary(event, T - 60, T + 620), state)
    # The gap-jumping tick: well past the timeout relative to the latch, but the FIRST event
    # to say so — the same tick production would close the trip on.
    assert eng.decide(_fix(event, T + 620 + 2100), state) is None           # no premature emit
    d = eng.decide(_primary(event, T, T + 600), state)                      # recursed trip lands
    assert d is not None
    assert "car_trip" in [s["message"]["name"] for s in d.sources]          # paired, consumed
    assert state.get("pending") == []


def test_a_late_session_is_absorbed_not_double_emitted(state, event):
    """When the trip fires first (an exit scored after the arrival confirm), the late session
    must not become a second journey — the event is immutable, so the corroboration is lost
    (measurably) rather than the timeline gaining a duplicate."""
    eng = _fusion()
    assert eng.decide(_primary(event, T, T + 600), state) is not None       # journey emitted
    eng.decide(_secondary(event, T - 60, T + 620), state)                   # session lands late
    assert state.get("pending") == []                                       # absorbed
    assert eng.decide(_fix(event, T + 9000), state) is None                 # and never emits


def test_a_phantom_session_is_refuted_by_live_geometry(state, event):
    """The union fallback must not re-import car_trip's phantom family: a lock+door pair at
    home mints a car_trip while location fixes flow straight through its span — geometry was
    watching and saw no journey, so the claim is refuted (ADR 0009's veto), not emitted."""
    eng = _fusion()
    assert eng.decide(_fix(event, T + 100), state) is None                  # geometry is alive
    eng.decide(_secondary(event, T, T + 600), state)                        # phantom claim
    assert eng.decide(_fix(event, T + 3000), state) is None
    assert eng.decide(_fix(event, T + 3100), state) is None                 # expiry comes up...
    assert eng.decide(_fix(event, T + 3200), state) is None                 # ...and drops it
    assert state.get("pending") == []                                       # gone, not emitted


def test_ticks_alone_never_fire(state, event):
    eng = _fusion()
    assert eng.decide(_fix(event, T), state) is None
    assert eng.decide(_fix(event, T + 3600), state) is None
