"""Tests for the BMW CarData descriptor→signal mapper (ADR 0006).

The mapper lives in `workers/bmw-cardata/` (a producer, not part of the installed
`inference` package) but is pure stdlib and edge-triggered — exactly the kind of logic worth
pinning. Loaded by path so no packaging change is needed; it has no intra-package imports.
"""

import importlib.util
import pathlib

import pytest

_MAPPER_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "workers" / "bmw-cardata" / "bmw_cardata" / "mapper.py"
)


def _load_mapper_module():
    spec = importlib.util.spec_from_file_location("bmw_mapper_under_test", _MAPPER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mapper_mod = _load_mapper_module()


def _msg(descriptor, value, ts="2026-07-26T10:00:00Z"):
    """One CarData MQTT message carrying a single descriptor update."""
    return {"vin": "V", "data": [{"name": descriptor, "value": value, "timestamp": ts}]}


@pytest.fixture
def mapper():
    return mapper_mod.Mapper()


def _names(emitted):
    return [name for name, _ts, _extra in emitted]


# --- edge triggering -------------------------------------------------------------------

def test_first_observation_is_a_silent_baseline(mapper):
    """A parked car's initial/retained state must not mint a phantom event on reconnect."""
    assert mapper.process(_msg(mapper_mod.DESCRIPTOR_DRIVER_DOOR, "CLOSED")) == []
    assert mapper.process(_msg(mapper_mod.DESCRIPTOR_MOTION, "false")) == []


def test_unchanged_value_emits_nothing(mapper):
    mapper.process(_msg(mapper_mod.DESCRIPTOR_DRIVER_DOOR, "CLOSED"))
    mapper.process(_msg(mapper_mod.DESCRIPTOR_DRIVER_DOOR, "OPEN"))
    assert _names(mapper.process(_msg(mapper_mod.DESCRIPTOR_DRIVER_DOOR, "OPEN"))) == []


def test_driver_door_emits_on_the_open_edge_only(mapper):
    mapper.process(_msg(mapper_mod.DESCRIPTOR_DRIVER_DOOR, "CLOSED"))
    assert _names(mapper.process(_msg(mapper_mod.DESCRIPTOR_DRIVER_DOOR, "OPEN"))) == [
        mapper_mod.SIG_DOOR_OPEN
    ]
    # The close edge is a state change but carries no signal we use.
    assert _names(mapper.process(_msg(mapper_mod.DESCRIPTOR_DRIVER_DOOR, "CLOSED"))) == []


def test_event_timestamp_comes_from_the_descriptor_not_wall_clock(mapper):
    mapper.process(_msg(mapper_mod.DESCRIPTOR_DRIVER_DOOR, "CLOSED"))
    (_name, ts, _extra), = mapper.process(
        _msg(mapper_mod.DESCRIPTOR_DRIVER_DOOR, "OPEN", ts="2026-07-26T10:15:30Z")
    )
    assert ts == 1785060930


# --- lock status: car-native AND directional (2026-07-26) ------------------------------

def test_lock_status_is_directional(mapper):
    """SECURED/UNLOCKED are distinct signals — the point of adding this descriptor."""
    mapper.process(_msg(mapper_mod.DESCRIPTOR_LOCK, "SECURED"))  # baseline
    assert _names(mapper.process(_msg(mapper_mod.DESCRIPTOR_LOCK, "UNLOCKED"))) == [
        mapper_mod.SIG_UNLOCKED
    ]
    assert _names(mapper.process(_msg(mapper_mod.DESCRIPTOR_LOCK, "SECURED"))) == [
        mapper_mod.SIG_LOCKED
    ]


def test_unrecognized_lock_state_emits_nothing_and_is_reported(mapper, caplog):
    """SELECTIVE_LOCKED & friends are deliberately unmapped — log them, don't guess."""
    with caplog.at_level("INFO"):
        assert mapper.process(_msg(mapper_mod.DESCRIPTOR_LOCK, "SELECTIVE_LOCKED")) == []
    assert "SELECTIVE_LOCKED" in caplog.text


def test_unrecognized_lock_state_does_not_disturb_the_baseline(mapper):
    mapper.process(_msg(mapper_mod.DESCRIPTOR_LOCK, "SECURED"))
    mapper.process(_msg(mapper_mod.DESCRIPTOR_LOCK, "SELECTIVE_LOCKED"))
    # Still a SECURED→UNLOCKED edge: the unreadable value must not have become the baseline.
    assert _names(mapper.process(_msg(mapper_mod.DESCRIPTOR_LOCK, "UNLOCKED"))) == [
        mapper_mod.SIG_UNLOCKED
    ]


# --- the stream inventory ---------------------------------------------------------------

def test_unmapped_descriptor_is_logged_with_its_value(mapper, caplog):
    """The odometer/GPS case: value included, because the value IS the finding."""
    with caplog.at_level("INFO"):
        assert mapper.process(_msg("vehicle.vehicle.travelledDistance", 48213)) == []
    assert "vehicle.vehicle.travelledDistance" in caplog.text
    assert "48213" in caplog.text


def test_unmapped_descriptor_is_logged_once_per_descriptor(mapper, caplog):
    """Permanent-on means once per id, not once per message — the stream must stay readable."""
    with caplog.at_level("INFO"):
        for _ in range(5):
            mapper.process(_msg("vehicle.cabin.window.row1.driver.isOpen", "CLOSED"))
        mapper.process(_msg("vehicle.body.hood.isOpen", "CLOSED"))
    unmapped = [r for r in caplog.records if "UNMAPPED" in r.getMessage()]
    assert len(unmapped) == 2


def test_unmapped_descriptor_is_logged_even_when_seen_only_once(mapper, caplog):
    """Inventory happens before the baseline check, or a once-only descriptor stays invisible."""
    with caplog.at_level("INFO"):
        mapper.process(_msg("vehicle.drivetrain.fuel.percentage", 61))
    assert "vehicle.drivetrain.fuel.percentage" in caplog.text


def test_one_message_can_carry_a_whole_batch(mapper):
    """This X1 sends state-change batches around wake/park — all edges in one message."""
    mapper.process(
        {
            "vin": "V",
            "data": [
                {"name": mapper_mod.DESCRIPTOR_DRIVER_DOOR, "value": "CLOSED", "timestamp": 1785060000},
                {"name": mapper_mod.DESCRIPTOR_LOCK, "value": "SECURED", "timestamp": 1785060000},
            ],
        }
    )
    emitted = mapper.process(
        {
            "vin": "V",
            "data": [
                {"name": mapper_mod.DESCRIPTOR_DRIVER_DOOR, "value": "OPEN", "timestamp": 1785060060},
                {"name": mapper_mod.DESCRIPTOR_LOCK, "value": "UNLOCKED", "timestamp": 1785060060},
                {"name": "vehicle.vehicle.travelledDistance", "value": 48213, "timestamp": 1785060060},
            ],
        }
    )
    assert _names(emitted) == [mapper_mod.SIG_DOOR_OPEN, mapper_mod.SIG_UNLOCKED]
