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


# --- got_into_the_car charger anchor (ADR 0005, 2026-07-17 CarPlay-flap revision) ---

# Event-time base far enough past epoch 0 that the first fire clears the cooldown.
_T = 1_700_000_000


def test_got_into_fires_on_charger_plus_carplay_without_lock(event, state):
    """The 2026-07-16 case: entry with CarPlay + charger but no lock-change still opens a trip
    (power 6 + CarPlay 5 = 11 >= 11)."""
    router = Router(RoutingPlan.from_definitions(load_definitions(REPO_ROOT / "events")))
    router.route(event("device_connected_to_carplay", _T, id="C"), state)
    out = router.route(event("device_connected_to_power", _T + 20, id="P"), state)
    assert "got_into_the_car" in {i["message"]["name"] for i in out}


def test_got_into_does_not_fire_on_carplay_plus_lock_without_charger(event, state):
    """The park-and-settle CarPlay flap: a lock-change + a transient CarPlay-connect (no charger)
    must NOT open a trip (CarPlay 5 + lock 5 = 10 < 11). Under the old 5/5/5 weights this fired a
    phantom trip that cooldown-swallowed the real one (ADR 0005 charger-anchor revision)."""
    router = Router(RoutingPlan.from_definitions(load_definitions(REPO_ROOT / "events")))
    router.route(event("car_lock_state_change", _T, id="L"), state)
    out = router.route(event("device_connected_to_carplay", _T + 80, id="C"), state)
    assert "got_into_the_car" not in {i["message"]["name"] for i in out}


def test_carplay_flap_defers_got_into_to_charger_connect(event, state):
    """Full park-and-settle: a lock-change then several CarPlay connect/disconnect bounces never
    open a trip; got_into fires only once the charger connects (settled in to drive), and no
    phantom car_trip forms in the meantime."""
    router = Router(RoutingPlan.from_definitions(load_definitions(REPO_ROOT / "events")))
    router.route(event("car_lock_state_change", _T, id="L"), state)
    during = []
    for i, t in enumerate((20, 40, 150, 200)):
        during += router.route(event("device_connected_to_carplay", _T + t, id=f"C{i}"), state)
        during += router.route(event("device_disconnected_from_carplay", _T + t + 8, id=f"D{i}"), state)
    names = {i["message"]["name"] for i in during}
    assert "got_into_the_car" not in names   # the flap alone never opens a trip
    assert "car_trip" not in names           # so no phantom trip forms
    out = router.route(event("device_connected_to_power", _T + 260, id="P"), state)
    assert "got_into_the_car" in {i["message"]["name"] for i in out}   # charger settles it
