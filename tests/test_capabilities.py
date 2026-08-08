"""Capability derivers: interval derives generically from the source events' extent."""

import pytest

from inference.event import Capability
from inference import capabilities
from inference.capabilities import derive_capability


def _src(ts, id="i"):
    return {"message": {"id": id, "name": "x", "timestamp": ts}}


def test_interval_spans_min_to_max_of_sources():
    frag = derive_capability(Capability.INTERVAL, [_src(300), _src(100), _src(200)])
    iv = frag["interval"]
    assert (iv.started_at, iv.ended_at, iv.duration_seconds) == (100, 300, 200)


def test_unknown_capability_raises():
    with pytest.raises(RuntimeError):
        derive_capability("not_a_capability", [_src(1)])


# --- place ----------------------------------------------------------------------

def _fix(lat, lon, ts=100):
    return {"message": {"id": "i", "name": "location_ping", "timestamp": ts, "lat": lat, "lon": lon}}


# Two real home fixes ~9m apart, and a shop ~4km away.
_H1, _H2 = (47.20694, 8.57468), (47.20702, 8.57472)
_SHOP = (47.194887, 8.523353)


def test_place_derives_centroid_and_spread_without_any_reference_data():
    """The centroid half is pure: an event at an unlisted place still knows WHERE it was."""
    capabilities.set_place_book([])
    pl = derive_capability(Capability.PLACE, [_fix(*_H1), _fix(*_H2)])["place"]
    assert pytest.approx(pl.lat, abs=1e-5) == 47.20698
    assert pytest.approx(pl.lon, abs=1e-5) == 8.574700
    assert 0 < pl.spread_m < 10                      # half the ~9m separation
    assert pl.label is None and pl.distance_m is None


def test_place_labels_a_stay_inside_a_known_place():
    capabilities.set_place_book([{"name": "Home", "lat": 47.206985, "lon": 8.574798, "radius_m": 80}])
    pl = derive_capability(Capability.PLACE, [_fix(*_H1), _fix(*_H2)])["place"]
    assert pl.label == "Home" and pl.distance_m < 20


def test_place_carries_the_everyday_flag_from_the_matched_place():
    """`everyday` marks the place you LIVE in, whose stays have no natural boundaries — the
    runtime still derives them, and the flag lets a consumer skip them without the engine
    having an opinion about what to draw."""
    capabilities.set_place_book([
        {"name": "Home", "lat": 47.206985, "lon": 8.574798, "radius_m": 80, "everyday": True},
        {"name": "Shop", "lat": 47.194887, "lon": 8.523353, "radius_m": 80, "everyday": False},
    ])
    assert derive_capability(Capability.PLACE, [_fix(*_H1)])["place"].everyday is True
    assert derive_capability(Capability.PLACE, [_fix(*_SHOP)])["place"].everyday is False


def test_place_everyday_defaults_to_false_for_a_row_that_omits_it():
    """The column is new; a book assembled without it must not make every place everyday."""
    capabilities.set_place_book([{"name": "Home", "lat": 47.206985, "lon": 8.574798, "radius_m": 80}])
    assert derive_capability(Capability.PLACE, [_fix(*_H1)])["place"].everyday is False


def test_place_everyday_is_none_when_nothing_matched():
    """An unlabelled stay makes no claim either way — it isn't 'not everyday', it's unknown."""
    capabilities.set_place_book([{"name": "Home", "lat": 47.206985, "lon": 8.574798,
                                 "radius_m": 80, "everyday": True}])
    assert derive_capability(Capability.PLACE, [_fix(*_SHOP)])["place"].everyday is None


def test_place_leaves_an_unknown_place_unlabelled_but_located():
    capabilities.set_place_book([{"name": "Home", "lat": 47.206985, "lon": 8.574798, "radius_m": 80}])
    pl = derive_capability(Capability.PLACE, [_fix(*_SHOP)])["place"]
    assert pl.label is None                          # 4km away -> no match
    assert pytest.approx(pl.lat, abs=1e-6) == _SHOP[0]   # ...but still located


def test_place_nearest_match_wins_when_radii_overlap():
    """A shop inside a declared district must label the shop, not the district."""
    capabilities.set_place_book([
        {"name": "District", "lat": 47.2000, "lon": 8.5500, "radius_m": 5000},
        {"name": "Shop", "lat": 47.194887, "lon": 8.523353, "radius_m": 80},
    ])
    assert derive_capability(Capability.PLACE, [_fix(*_SHOP)])["place"].label == "Shop"


def test_place_absent_when_no_source_carries_coordinates():
    """Declaring the capability on a non-geo definition is a visible no-op, not a fake point."""
    capabilities.set_place_book([])
    assert derive_capability(Capability.PLACE, [_src(100), _src(200)]) == {}


def test_place_survives_a_malformed_reference_row():
    capabilities.set_place_book([{"name": "Broken"}, {"name": "Home", "lat": 47.206985,
                                                      "lon": 8.574798, "radius_m": 80}])
    assert derive_capability(Capability.PLACE, [_fix(*_H1)])["place"].label == "Home"


# --- journey --------------------------------------------------------------------

# The 2026-07-30 vet trip, abridged: home -> two points down the road -> the clinic.
_VET = (47.160316, 8.441387)
_MID1, _MID2 = (47.19, 8.52), (47.17, 8.47)


def _leg_fix(pt, ts, motion=None):
    msg = {"id": f"i{ts}", "name": "location_ping", "timestamp": ts, "lat": pt[0], "lon": pt[1]}
    if motion is not None:
        msg["motion"] = motion
    return {"message": msg}


def test_journey_labels_both_endpoints_against_the_place_book():
    """The whole point: the trip that motivated the engine reads Home -> the clinic, which is
    what `place` (one centroid over a 24km drive) structurally cannot say."""
    capabilities.set_place_book([
        {"name": "Home", "lat": 47.206985, "lon": 8.574798, "radius_m": 80, "everyday": True},
        {"name": "ENNETSeeKLINIK", "lat": 47.16031, "lon": 8.44138, "radius_m": 80},
    ])
    j = derive_capability(Capability.JOURNEY, [
        _leg_fix(_H1, 100), _leg_fix(_MID1, 200), _leg_fix(_MID2, 300), _leg_fix(_VET, 400),
    ])["journey"]
    assert j.origin.label == "Home" and j.origin.everyday is True
    assert j.destination.label == "ENNETSeeKLINIK"
    assert j.origin.spread_m == 0.0                      # an endpoint is one fix, not a cluster


def test_journey_endpoints_come_from_event_time_not_list_order():
    """The order an engine appended in is an implementation detail; earliest/latest is a fact
    about the evidence (the same reasoning as _interval)."""
    capabilities.set_place_book([])
    shuffled = [_leg_fix(_MID2, 300), _leg_fix(_VET, 400), _leg_fix(_H1, 100), _leg_fix(_MID1, 200)]
    j = derive_capability(Capability.JOURNEY, shuffled)["journey"]
    assert (j.origin.lat, j.origin.lon) == _H1
    assert (j.destination.lat, j.destination.lon) == _VET


def test_journey_distinguishes_a_loop_from_a_transfer():
    """A drive out and back has ~zero straight-line distance and a large path. Reporting only
    the first would call a real journey a non-journey."""
    capabilities.set_place_book([])
    j = derive_capability(Capability.JOURNEY, [
        _leg_fix(_H1, 100), _leg_fix(_VET, 200), _leg_fix(_H1, 300),
    ])["journey"]
    assert j.straight_line_m == 0.0
    assert j.path_m > 20_000


def test_journey_mode_is_the_streams_majority_moving_claim():
    """Read from the phone's own classifier, and `stationary` excluded — a journey's endpoints
    are settled by construction, so counting them would let a traffic jam relabel a drive."""
    capabilities.set_place_book([])
    sources = [
        _leg_fix(_H1, 100, ["stationary"]),
        _leg_fix(_MID1, 200, ["driving"]),
        _leg_fix(_MID2, 300, ["driving"]),
        _leg_fix(_VET, 400, ["stationary"]),
    ]
    assert derive_capability(Capability.JOURNEY, sources)["journey"].mode == "driving"


def test_journey_mode_is_none_when_nothing_claimed_one():
    """Honest: the journey happened, we just can't say how."""
    capabilities.set_place_book([])
    j = derive_capability(Capability.JOURNEY, [_leg_fix(_H1, 100), _leg_fix(_VET, 200)])["journey"]
    assert j.mode is None


def test_journey_needs_two_fixes():
    """A single point is not a journey; no fragment beats a fabricated one."""
    capabilities.set_place_book([])
    assert derive_capability(Capability.JOURNEY, [_leg_fix(_H1, 100)]) == {}
    assert derive_capability(Capability.JOURNEY, [{"message": {"timestamp": 1}}]) == {}


# --- vehicle --------------------------------------------------------------------
#
# The deriver classifies STRUCTURALLY: a source with coordinates is movement, one without is
# corroboration. So these fixtures never have to match a real event name for it to work.


def _mark(name, ts):
    """A corroborating source — no coordinates, which is what makes it corroboration."""
    return {"message": {"id": f"m{ts}", "name": name, "timestamp": ts}}


def test_vehicle_reports_the_corroborating_names_it_actually_found():
    v = derive_capability(Capability.VEHICLE, [
        _leg_fix(_H1, 100), _mark("got_into_the_car", 110),
        _leg_fix(_VET, 400), _mark("got_out_the_car", 390),
    ])["vehicle"]
    assert v.evidence == ["got_into_the_car", "got_out_the_car"]   # chronological
    assert v.confirmed is True


def test_vehicle_is_absent_without_corroboration():
    """The borrowed-car case: 6 of 6 such journeys had no boundary inside the span. No fragment
    rather than known=False — the peripherals could simply have been off."""
    assert derive_capability(Capability.VEHICLE, [_leg_fix(_H1, 100), _leg_fix(_VET, 400)]) == {}


def test_vehicle_one_signal_is_evidence_but_not_confirmed():
    """2 of the 14 own-car journeys had only an exit boundary inside the span."""
    v = derive_capability(Capability.VEHICLE, [
        _leg_fix(_H1, 100), _leg_fix(_VET, 400), _mark("got_out_the_car", 390),
    ])["vehicle"]
    assert v.evidence == ["got_out_the_car"] and v.confirmed is False


def test_vehicle_counts_distinct_signals_not_repeats():
    """A lock burst while unloading groceries is one kind of evidence, not three."""
    v = derive_capability(Capability.VEHICLE, [
        _leg_fix(_H1, 100),
        _mark("got_out_the_car", 300), _mark("got_out_the_car", 320), _mark("got_out_the_car", 340),
        _leg_fix(_VET, 400),
    ])["vehicle"]
    assert v.evidence == ["got_out_the_car"] and v.confirmed is False


def test_vehicle_never_learns_a_concrete_event_name():
    """Framework code must not name concrete signals — the deriver reports whatever it found,
    so a future corroborating source needs no change here."""
    v = derive_capability(Capability.VEHICLE, [
        _leg_fix(_H1, 100), _mark("bicycle_unlocked", 150), _mark("helmet_paired", 160),
        _leg_fix(_VET, 400),
    ])["vehicle"]
    assert v.evidence == ["bicycle_unlocked", "helmet_paired"] and v.confirmed is True


# --- interval: the span of a journey is the span of the MOVEMENT -----------------

def test_interval_uses_only_the_located_sources_when_any_are_present():
    """A `trip` carries both the fixes that define where it went and the car boundaries that
    prove whose car it was — and those boundaries sit OUTSIDE a correctly-measured journey
    (issue #44). Letting them set the bounds would redefine the span as get-in→get-out for
    own-car journeys while leaving it displacement-derived for borrowed ones."""
    frag = derive_capability(Capability.INTERVAL, [
        _mark("got_into_the_car", 50),          # 50s before the first fix
        _leg_fix(_H1, 100), _leg_fix(_VET, 400),
        _mark("got_out_the_car", 460),          # 60s after the last fix
    ])
    iv = frag["interval"]
    assert (iv.started_at, iv.ended_at, iv.duration_seconds) == (100, 400, 300)


def test_interval_falls_back_to_all_sources_when_none_are_located():
    """Unchanged for every other event: `car_trip`'s sources carry no coordinates at all,
    so it still spans its full lineage."""
    frag = derive_capability(Capability.INTERVAL, [
        _mark("got_into_the_car", 100), _mark("got_out_the_car", 700)])
    iv = frag["interval"]
    assert (iv.started_at, iv.ended_at) == (100, 700)


# --- support (ADR 0011) -----------------------------------------------------------
#
# Evidence kinds read structurally: located sources are the `geometry` kind, each derived
# source is a kind named by its event name — unless it is CONTAINED in another source's
# sidecar, in which case it is a constituent of that claim, not independent evidence.


def _located(ts, id):
    return {"message": {"id": id, "name": "location_ping", "timestamp": ts,
                        "lat": 47.2, "lon": 8.5}}


def _claim(name, ts, id, sources=()):
    return {"message": {"id": id, "name": name, "timestamp": ts, "user_id": "u",
                        "inference_type": "an_engine"},
            "sources": list(sources)}


def test_support_geometry_alone_is_single_source():
    frag = derive_capability(Capability.SUPPORT, [_located(100, "a"), _located(200, "b")])
    assert frag["support"].level == "single_source"
    assert frag["support"].evidence_kinds == ["geometry"]


def test_support_geometry_plus_a_claim_is_corroborated():
    frag = derive_capability(Capability.SUPPORT, [
        _located(100, "a"), _located(200, "b"), _claim("car_trip", 210, "ct")])
    assert frag["support"].level == "corroborated"
    assert frag["support"].evidence_kinds == ["geometry", "car_trip"]


def test_support_collapses_a_claims_constituents():
    """A car_trip arrives carrying its got_into/got_out in its sidecar (the ADR 0011 recursion
    change); when those envelopes ALSO appear top-level (hoisted for interval, or as trip
    marks), they must not count as extra kinds — one physical detector lane, one vote."""
    into = _claim("got_into_the_car", 90, "in")
    out = _claim("got_out_the_car", 200, "out")
    frag = derive_capability(Capability.SUPPORT, [
        _claim("car_trip", 200, "ct", sources=[into, out]), into, out])
    assert frag["support"].level == "single_source"
    assert frag["support"].evidence_kinds == ["car_trip"]


def test_support_session_only_with_geometry_absent():
    into = _claim("got_into_the_car", 90, "in")
    out = _claim("got_out_the_car", 200, "out")
    frag = derive_capability(Capability.SUPPORT, [
        _claim("car_trip", 200, "ct", sources=[into, out]), into, out])
    assert "geometry" not in frag["support"].evidence_kinds


def test_support_asserts_nothing_over_raw_unlocated_evidence():
    frag = derive_capability(Capability.SUPPORT, [_src(100), _src(200)])
    assert frag == {}
