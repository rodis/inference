"""The transport-agnostic core: plan resolution, keying, detection+recursion, shaping."""

import pytest

from inference.event import Capability
from inference.runtime.core import RoutingPlan, Router, Shaper


# --- RoutingPlan ----------------------------------------------------------------

def test_plan_indexes_consumers_and_capabilities(definition):
    d = definition("car_trip", "session_window", {"start_event": "in", "end_event": "out"},
                   capabilities=[Capability.INTERVAL])
    plan = RoutingPlan.from_definitions([d])
    assert set(plan.consumers) == {"in", "out"}
    assert plan.consumers["in"][0].produces == "car_trip"
    assert plan.capabilities_for["car_trip"] == (Capability.INTERVAL,)
    assert plan.source_topic == "raw_sensors"
    assert plan.sink_topics == {"high_level_events"}


def test_plan_requires_exactly_one_external_source(definition):
    a = definition("a", "weighted_window", {"weights": {"x": 10}, "threshold": 10, "window_seconds": 10})
    b = definition("b", "weighted_window", {"weights": {"y": 10}, "threshold": 10, "window_seconds": 10},
                   source_topic="another_topic")
    with pytest.raises(RuntimeError):
        RoutingPlan.from_definitions([a, b])                        # two external sources


def test_plan_rejects_zero_external_source(definition):
    d = definition("a", "weighted_window", {"weights": {"x": 10}, "threshold": 10, "window_seconds": 10},
                   source_topic="high_level_events", sink_topic="high_level_events")
    with pytest.raises(RuntimeError):
        RoutingPlan.from_definitions([d])                           # source is our own sink


# --- Router.key_for -------------------------------------------------------------

def test_key_for_uses_user_id(event):
    assert Router.key_for(event("x", 1, user_id="rods")) == "rods"


def test_key_for_missing_user_id_falls_back_to_sentinel():
    assert Router.key_for({"message": {"name": "x", "timestamp": 1}}) == "_no_user_id"


# --- Router.route (detection only) ----------------------------------------------

def test_route_emits_base_and_source_sidecar_without_shaping(definition, event, state):
    d = definition("car_trip", "session_window", {"start_event": "in", "end_event": "out"},
                   capabilities=[Capability.INTERVAL])
    router = Router(RoutingPlan.from_definitions([d]))
    assert router.route(event("in", 1000, id="S"), state) == []     # start only — no fire yet
    out = router.route(event("out", 1600, id="E"), state)
    assert len(out) == 1
    item = out[0]
    assert item["message"]["name"] == "car_trip"
    assert len(item["sources"]) == 2                                # full bodies carried forward
    # detection stays out of shaping: no role/lineage/capability on the routed envelope
    for shaped_field in ("role", "derived_from", "interval"):
        assert shaped_field not in item["message"]


def test_route_resolves_recursion_in_process_without_nesting_sources(definition, event, state):
    mid = definition("mid", "weighted_window",
                     {"weights": {"sig": 10}, "threshold": 10, "window_seconds": 600, "cooldown_seconds": 0})
    top = definition("top", "weighted_window",
                     {"weights": {"mid": 10}, "threshold": 10, "window_seconds": 600, "cooldown_seconds": 0})
    router = Router(RoutingPlan.from_definitions([mid, top]))
    out = router.route(event("sig", 100, id="R"), state)
    assert {i["message"]["name"] for i in out} == {"mid", "top"}    # one raw signal cascades two levels
    top_item = next(i for i in out if i["message"]["name"] == "top")
    assert all("sources" not in s for s in top_item["sources"])     # recursed on the CLEAN envelope


# --- Shaper.shape ---------------------------------------------------------------

def test_shape_projects_lineage_and_derives_declared_capability(definition):
    d = definition("car_trip", "session_window", {"start_event": "in", "end_event": "out"},
                   capabilities=[Capability.INTERVAL])
    shaper = Shaper(RoutingPlan.from_definitions([d]))
    item = {
        "message": {"id": "T", "name": "car_trip", "inference_type": "session_window",
                    "user_id": "u", "timestamp": 1600, "confidence_score": 1.0},
        "sources": [
            {"message": {"id": "S", "name": "in", "timestamp": 1000, "extra": "drop-me"}},
            {"message": {"id": "E", "name": "out", "timestamp": 1600}},
        ],
    }
    rec = shaper.shape(item)
    assert rec["name"] == "car_trip" and rec["source_type"] == "kafka"
    msg = rec["message"]
    # lineage is the trimmed projection — the source's `extra` body field is dropped
    assert msg["derived_from"] == [
        {"id": "S", "name": "in", "timestamp": 1000},
        {"id": "E", "name": "out", "timestamp": 1600},
    ]
    # interval capability derived from the full sources' extent
    assert msg["interval"] == {"started_at": 1000, "ended_at": 1600, "duration_seconds": 600}
    assert "role" not in msg


def test_shape_without_capability_has_null_interval(definition):
    d = definition("door", "weighted_window",
                   {"weights": {"x": 10}, "threshold": 10, "window_seconds": 10})   # no capabilities
    shaper = Shaper(RoutingPlan.from_definitions([d]))
    item = {
        "message": {"id": "D", "name": "door", "inference_type": "weighted_window",
                    "user_id": "u", "timestamp": 5, "confidence_score": 10.0},
        "sources": [{"message": {"id": "x", "name": "x", "timestamp": 5}}],
    }
    assert shaper.shape(item)["message"]["interval"] is None
