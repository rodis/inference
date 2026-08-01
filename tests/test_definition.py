"""Definition loading — best-effort, isolated; and the real events/ build a valid plan."""

from pathlib import Path

from inference.runtime.core import Router, RoutingPlan
from inference.runtime.definition import load_definitions

REPO_ROOT = Path(__file__).resolve().parent.parent

_GOOD = """
name: good
engine: weighted_window
engine_config: {}
source_topic: raw_sensors
sink_topic: high_level_events
"""
_DISABLED = """
name: off
enabled: false
engine: weighted_window
engine_config: {}
source_topic: raw_sensors
sink_topic: high_level_events
"""
_INVALID = """
name: bad
engine: weighted_window
"""  # missing required source_topic / sink_topic


def test_load_skips_disabled_and_invalid(tmp_path):
    (tmp_path / "good.yml").write_text(_GOOD)
    (tmp_path / "off.yml").write_text(_DISABLED)
    (tmp_path / "bad.yml").write_text(_INVALID)
    defs = load_definitions(tmp_path)
    assert [d.name for d in defs] == ["good"]   # disabled + invalid dropped, valid kept


def test_real_definitions_build_a_valid_plan():
    defs = load_definitions(REPO_ROOT / "events")
    assert defs, "no event definitions loaded"
    plan = RoutingPlan.from_definitions(defs)
    assert plan.source_topic == "raw_sensors"
    assert "high_level_events" in plan.sink_topics


# --- got_into_the_car CarPlay anchor (issue #39, 2026-08-02 charger retirement) ---

# Event-time base far enough past epoch 0 that the first fire clears the cooldown.
_T = 1_700_000_000


def test_got_into_fires_on_carplay_plus_door_without_lock(event, state):
    """The 2026-07-16 shape: entry with no lock-change still opens a trip — the anchor plus one
    corroborator reaches threshold (CarPlay 6 + door 4 = 10 >= 10)."""
    router = Router(RoutingPlan.from_definitions(load_definitions(REPO_ROOT / "events")))
    router.route(event("device_connected_to_carplay", _T, id="C"), state)
    out = router.route(event("car_driver_door_opened", _T + 20, id="D"), state)
    assert "got_into_the_car" in {i["message"]["name"] for i in out}


def test_got_into_fires_on_carplay_plus_lock(event, state):
    """CarPlay + lock = 10 >= 10 fires. This pair was deliberately blocked in the charger-anchor
    era (park-and-settle CarPlay flap); with the charger retired it is a legitimate entry —
    measured 0 junk trips over the post-BMW-door window (issue #39)."""
    router = Router(RoutingPlan.from_definitions(load_definitions(REPO_ROOT / "events")))
    router.route(event("car_lock_state_change", _T, id="L"), state)
    out = router.route(event("device_connected_to_carplay", _T + 80, id="C"), state)
    assert "got_into_the_car" in {i["message"]["name"] for i in out}


def test_got_into_does_not_fire_on_lock_plus_door(event, state):
    """lock + door = 8 < 10: the two direction-ambiguous signals are the EXIT combination, so
    between themselves they must never mint an entry — that is the phantom-entry-at-exit class
    (ADR 0005; the door-as-anchor candidate that fired on this pair minted 16 junk trips in
    8 days, issue #39)."""
    router = Router(RoutingPlan.from_definitions(load_definitions(REPO_ROOT / "events")))
    router.route(event("car_lock_state_change", _T, id="L"), state)
    out = router.route(event("car_driver_door_opened", _T + 20, id="D"), state)
    names = {i["message"]["name"] for i in out}
    assert "got_into_the_car" not in names
    assert "car_trip" not in names           # so no phantom trip can form either
