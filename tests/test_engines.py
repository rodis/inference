"""Engine decide() logic — thresholds, cooldown, windowing, decay, pairing, and that
each engine now carries the FULL source event bodies on the Decision (not {id,ts})."""

from inference.engines.decaying_window import DecayingWindowEngine
from inference.engines.geofence import GeofenceEngine
from inference.engines.session_gated_window import SessionGatedWindowEngine
from inference.engines.session_window import SessionWindowEngine
from inference.engines.stay_window import StayWindowEngine
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


# --- geofence -------------------------------------------------------------------

# Region centre (a real fix the phone reported) + a point ~11km away that is clearly out.
_IN = dict(lat=47.2069, lon=8.5748)
_OUT = dict(lat=47.30, lon=8.70)


def _geofence(direction, **over):
    cfg = {"lat": 47.2069, "lon": 8.5748, "radius_m": 150, "direction": direction, "owner": "rods"}
    cfg.update(over)
    return GeofenceEngine(cfg)


def _ping(event, t, **over):
    kw = {"user_id": "rods", "acc": 10, **_IN, **over}
    return event("location_ping", t, **kw)


def test_geofence_enter_fires_once_on_boundary_cross(state, event):
    eng = _geofence("enter")
    assert eng.decide(_ping(event, T, **_OUT), state) is None          # outside -> no fire
    # _OUT is ~14km away, so allow a plausible travel time: the engine rejects fixes implying
    # impossible speed, and 14km in 60s (840km/h) is a bad fix, not a boundary crossing.
    d = eng.decide(_ping(event, T + 3600), state)                      # crossed in -> fire
    assert d is not None and d.occurred_at == T + 3600
    assert eng.decide(_ping(event, T + 3660), state) is None           # still inside -> no re-fire


def test_geofence_leave_fires_on_exit(state, event):
    eng = _geofence("leave")
    assert eng.decide(_ping(event, T), state) is None                  # inside first -> leave doesn't fire
    assert eng.decide(_ping(event, T + 3600, **_OUT), state) is not None  # inside -> outside -> fire


def test_geofence_ignores_other_users(state, event):
    eng = _geofence("enter")
    assert eng.decide(_ping(event, T, user_id="alice"), state) is None  # not the region owner


def test_geofence_accuracy_gate_ignores_imprecise_points(state, event):
    eng = _geofence("enter", radius_m=100)                             # max_accuracy defaults to radius
    assert eng.decide(_ping(event, T, acc=500), state) is None         # too vague -> ignored, state untouched
    assert eng.decide(_ping(event, T + 60, acc=10), state) is not None  # precise inside -> fires (state wasn't flipped)


def test_geofence_rejects_a_confidently_wrong_fix(state, event):
    """A fix can claim excellent accuracy and be a long way wrong (700m in one second,
    observed 2026-07-25) — reported accuracy is not a safety net, so containment must also
    reject physically impossible travel from the last accepted fix."""
    eng = _geofence("leave")
    assert eng.decide(_ping(event, T), state) is None                  # inside, accepted
    # 1s later, 11km away, and it insists acc=5 -> implies ~40M km/h -> not a real move
    assert eng.decide(_ping(event, T + 1, acc=5, **_OUT), state) is None
    # a plausible move out later still fires: state was never corrupted by the bad fix
    assert eng.decide(_ping(event, T + 3600, **_OUT), state) is not None


def test_geofence_writes_state_only_on_change(state, event):
    """A dense stream would otherwise write `inside` on every ping for every region, which is
    pure changelog traffic (Quix State = RocksDB + changelog)."""
    eng = _geofence("enter")
    eng.decide(_ping(event, T, **_OUT), state)                         # outside: no change from default
    writes = len(state._d)
    for i in range(5):
        eng.decide(_ping(event, T + 10 + i, **_OUT), state)            # still outside
    inside_writes = [k for k in state._d if k.endswith("inside")]
    assert inside_writes == []                                         # never flipped -> never written
    assert eng.decide(_ping(event, T + 3600), state) is not None       # crossing in writes it once
    assert [k for k in state._d if k.endswith("inside")] != []
    assert writes <= len(state._d)


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
