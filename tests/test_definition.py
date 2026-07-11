"""Definition loading — best-effort, isolated; and the real events/ build a valid plan."""

from pathlib import Path

from inference.runtime.core import RoutingPlan
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
