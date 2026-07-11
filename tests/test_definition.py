"""Definition loading — best-effort, isolated; and the real events/ build a valid plan."""

from pathlib import Path

from inference.runtime.core import Router, RoutingPlan
from inference.runtime.definition import load_definitions
from inference.runtime.regions import region_definitions

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


def test_real_gym_visit_pairs_owntracks_zone_events(event, state):
    """The OwnTracks Vector lane emits entered_gym / left_gym (slugified from the "Gym"
    waypoint desc); the real gym_visit def must consume exactly those names. This guards
    the ingest-slug <-> def-name contract that spans two components (Vector VRL + YAML)."""
    defs = load_definitions(REPO_ROOT / "events")
    router = Router(RoutingPlan.from_definitions(defs))
    assert router.route(event("entered_gym", 1000, id="S"), state) == []   # open the session
    out = router.route(event("left_gym", 4600, id="E"), state)
    gym = [i for i in out if i["message"]["name"] == "gym_visit"]
    assert len(gym) == 1 and len(gym[0]["sources"]) == 2


# --- regions -> geofence definitions --------------------------------------------

def test_region_rows_expand_to_geofence_definitions():
    rows = [{"user_id": "rods", "name": "Gym", "lat": 47.2, "lon": 8.5, "radius_m": 150}]
    defs = region_definitions(rows)
    assert [d.name for d in defs] == ["entered_gym", "left_gym"]
    assert all(d.engine == "geofence" and d.source_topic == "raw_sensors" for d in defs)
    assert defs[0].engine_config["direction"] == "enter"
    assert defs[0].engine_config["owner"] == "rods"


def test_location_stream_cascades_through_geofence_to_gym_visit(event, state):
    """Full server-side path in-memory: a location stream crosses a Neon-defined region,
    the geofence engine emits entered_gym/left_gym, and the real gym_visit session pairs
    them — proving regions + existing defs compose via in-process recursion, no phone."""
    rows = [{"user_id": "rods", "name": "Gym", "lat": 47.2069, "lon": 8.5748, "radius_m": 150}]
    defs = load_definitions(REPO_ROOT / "events") + region_definitions(rows)
    router = Router(RoutingPlan.from_definitions(defs))

    def ping(t, lat, lon):
        return event("location_ping", t, user_id="rods", lat=lat, lon=lon, acc=10)

    router.route(ping(1000, 47.30, 8.70), state)                       # outside — establishes state
    entered = router.route(ping(2000, 47.2069, 8.5748), state)         # cross in
    assert "entered_gym" in {i["message"]["name"] for i in entered}
    out = router.route(ping(5000, 47.30, 8.70), state)                 # cross out
    names = {i["message"]["name"] for i in out}
    assert "left_gym" in names and "gym_visit" in names                # visit fired via the cascade


# --- home-by-car derivations (geofence transitions + car activity) --------------

# Event-time base far enough past epoch 0 that the first fire clears the cooldown.
_T = 1_700_000_000


def test_left_home_by_car_fires_on_departure_pair(event, state):
    """got_into_the_car (derived) + left_home (raw geofence) co-occurring => left home by car."""
    router = Router(RoutingPlan.from_definitions(load_definitions(REPO_ROOT / "events")))
    assert router.route(event("got_into_the_car", _T, id="G"), state) == []      # half — no fire
    out = router.route(event("left_home", _T + 120, id="L"), state)              # departure completes
    assert "left_home_by_car" in {i["message"]["name"] for i in out}


def test_arrived_home_by_car_fires_on_arrival_pair(event, state):
    """entered_home (raw geofence) + got_out_the_car (derived) co-occurring => arrived home by car."""
    router = Router(RoutingPlan.from_definitions(load_definitions(REPO_ROOT / "events")))
    assert router.route(event("entered_home", _T, id="E"), state) == []          # half — no fire
    out = router.route(event("got_out_the_car", _T + 120, id="O"), state)        # arrival completes
    assert "arrived_home_by_car" in {i["message"]["name"] for i in out}


def test_left_home_by_car_does_not_fire_on_left_home_alone(event, state):
    """Leaving on foot (left_home without got_into_the_car) must NOT fire — the AND guard."""
    router = Router(RoutingPlan.from_definitions(load_definitions(REPO_ROOT / "events")))
    out = router.route(event("left_home", _T, id="L"), state)
    assert "left_home_by_car" not in {i["message"]["name"] for i in out}
