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

def test_descriptor_is_logged_with_its_value(mapper, caplog):
    """Value included, because for a numeric descriptor the value IS the finding."""
    with caplog.at_level("INFO"):
        mapper.process(_msg("vehicle.cabin.sunroof.status", "CLOSED"))
    assert "vehicle.cabin.sunroof.status" in caplog.text
    assert "CLOSED" in caplog.text


def test_descriptor_is_logged_once_per_descriptor(mapper, caplog):
    """Permanent-on means once per id, not once per message — the stream must stay readable."""
    with caplog.at_level("INFO"):
        for _ in range(5):
            mapper.process(_msg("vehicle.cabin.window.row1.driver.status", "CLOSED"))
        mapper.process(_msg("vehicle.body.hood.isOpen", "CLOSED"))
    seen = [r for r in caplog.records if "descriptor in stream" in r.getMessage()]
    assert len(seen) == 2


def test_descriptor_is_logged_even_when_seen_only_once(mapper, caplog):
    """Inventory happens before the baseline check, or a once-only descriptor stays invisible."""
    with caplog.at_level("INFO"):
        mapper.process(_msg("vehicle.body.trunk.isOpen", False))
    assert "vehicle.body.trunk.isOpen" in caplog.text


def test_mapped_but_unchanging_descriptor_is_still_inventoried(mapper, caplog):
    """How we tell "this X1 never sends isMoving" from "we never looked" — it emits nothing."""
    with caplog.at_level("INFO"):
        assert mapper.process(_msg(mapper_mod.DESCRIPTOR_MOTION, "false")) == []
    assert mapper_mod.DESCRIPTOR_MOTION in caplog.text


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
    assert _names(emitted) == [
        mapper_mod.SIG_DOOR_OPEN,
        mapper_mod.SIG_UNLOCKED,
        mapper_mod.SIG_ODOMETER,  # a reading rides along in the same batch
    ]


# --- readings: the value is the fact, so NOT baseline-silent (2026-07-27) ----------------

def _extra(emitted, name):
    return next(e for n, _ts, e in emitted if n == name)


def test_odometer_emits_on_first_observation_with_its_value(mapper):
    """Unlike an edge, a reading has no phantom to guard against — losing it costs a delta."""
    emitted = mapper.process(_msg(mapper_mod.DESCRIPTOR_ODOMETER, 24819))
    assert _names(emitted) == [mapper_mod.SIG_ODOMETER]
    assert _extra(emitted, mapper_mod.SIG_ODOMETER)["odometer_km"] == 24819


def test_repeated_identical_reading_emits_nothing(mapper):
    """De-duplication, not baseline-silence, is what stops a re-sent state dump spamming."""
    mapper.process(_msg(mapper_mod.DESCRIPTOR_ODOMETER, 24819))
    assert mapper.process(_msg(mapper_mod.DESCRIPTOR_ODOMETER, 24819)) == []
    assert _names(mapper.process(_msg(mapper_mod.DESCRIPTOR_ODOMETER, 24828))) == [
        mapper_mod.SIG_ODOMETER
    ]


def test_zero_odometer_is_a_reading_not_a_false(mapper):
    """Guards the _as_bool trap: 0 km must stay a number, not coerce to False and vanish."""
    emitted = mapper.process(_msg(mapper_mod.DESCRIPTOR_ODOMETER, 0))
    assert _extra(emitted, mapper_mod.SIG_ODOMETER)["odometer_km"] == 0


def test_fuel_level_is_a_reading(mapper):
    emitted = mapper.process(_msg(mapper_mod.DESCRIPTOR_FUEL, 18))
    assert _names(emitted) == [mapper_mod.SIG_FUEL]
    assert _extra(emitted, mapper_mod.SIG_FUEL)["fuel_level"] == 18


# --- GPS fusion --------------------------------------------------------------------------

def test_half_a_coordinate_emits_nothing(mapper):
    assert mapper.process(_msg(mapper_mod.DESCRIPTOR_GPS_LAT, 47.1715, ts=1785060000)) == []


def test_lat_and_lon_fuse_into_one_event(mapper):
    """One car_location per batch, not one per component — a lat alone locates nothing."""
    mapper.process(_msg(mapper_mod.DESCRIPTOR_GPS_LAT, 47.1715, ts=1785060000))
    emitted = mapper.process(_msg(mapper_mod.DESCRIPTOR_GPS_LON, 8.5163, ts=1785060001))
    assert _names(emitted) == [mapper_mod.SIG_LOCATION]
    extra = _extra(emitted, mapper_mod.SIG_LOCATION)
    assert (extra["lat"], extra["lon"]) == (47.1715, 8.5163)


def test_a_fresh_lat_does_not_pair_with_the_previous_parks_lon(mapper):
    """The window is the whole point: pairing across batches would invent a location."""
    mapper.process(_msg(mapper_mod.DESCRIPTOR_GPS_LAT, 47.1715, ts=1785060000))
    mapper.process(_msg(mapper_mod.DESCRIPTOR_GPS_LON, 8.5163, ts=1785060001))
    # Hours later the car parks elsewhere and latitude arrives first.
    assert mapper.process(_msg(mapper_mod.DESCRIPTOR_GPS_LAT, 47.4000, ts=1785070000)) == []
    assert _names(
        mapper.process(_msg(mapper_mod.DESCRIPTOR_GPS_LON, 8.6000, ts=1785070001))
    ) == [mapper_mod.SIG_LOCATION]


def test_an_unmoved_car_reports_no_new_location(mapper):
    """A reconnect re-sends the same point; that is not a move."""
    mapper.process(_msg(mapper_mod.DESCRIPTOR_GPS_LAT, 47.1715, ts=1785060000))
    mapper.process(_msg(mapper_mod.DESCRIPTOR_GPS_LON, 8.5163, ts=1785060001))
    mapper.process(_msg(mapper_mod.DESCRIPTOR_GPS_LAT, 47.1715, ts=1785060100))
    assert mapper.process(_msg(mapper_mod.DESCRIPTOR_GPS_LON, 8.5163, ts=1785060101)) == []


def test_altitude_rides_along_when_it_belongs_to_the_same_batch(mapper):
    mapper.process(_msg(mapper_mod.DESCRIPTOR_GPS_ALT, 423, ts=1785060000))
    mapper.process(_msg(mapper_mod.DESCRIPTOR_GPS_LAT, 47.1715, ts=1785060000))
    emitted = mapper.process(_msg(mapper_mod.DESCRIPTOR_GPS_LON, 8.5163, ts=1785060001))
    assert _extra(emitted, mapper_mod.SIG_LOCATION)["altitude"] == 423


def test_stale_altitude_is_left_out_rather_than_attached(mapper):
    mapper.process(_msg(mapper_mod.DESCRIPTOR_GPS_ALT, 423, ts=1785060000))
    mapper.process(_msg(mapper_mod.DESCRIPTOR_GPS_LAT, 47.4000, ts=1785070000))
    emitted = mapper.process(_msg(mapper_mod.DESCRIPTOR_GPS_LON, 8.6000, ts=1785070001))
    assert "altitude" not in _extra(emitted, mapper_mod.SIG_LOCATION)


# --- the catch-all: nothing is discarded any more -----------------------------------------

def test_unrecognized_descriptor_change_becomes_one_generic_event(mapper):
    """One event name for the whole tail of the stream, carrying descriptor + value."""
    mapper.process(_msg("vehicle.cabin.window.row1.driver.status", "CLOSED"))  # baseline
    emitted = mapper.process(_msg("vehicle.cabin.window.row1.driver.status", "OPEN"))
    assert _names(emitted) == [mapper_mod.SIG_STATE_CHANGE]
    assert _extra(emitted, mapper_mod.SIG_STATE_CHANGE) == {
        "descriptor": "vehicle.cabin.window.row1.driver.status",
        "value": "OPEN",
    }


def test_the_catch_all_is_baseline_silent(mapper):
    """Or the full state dump on every reconnect would emit ~20 events."""
    assert mapper.process(_msg("vehicle.vehicle.antiTheftAlarmSystem.alarm.armStatus", "armed")) == []


def test_the_catch_all_does_not_shadow_a_mapped_descriptor(mapper):
    """A door edge stays a door edge — it must not also surface as a generic state change."""
    mapper.process(_msg(mapper_mod.DESCRIPTOR_DRIVER_DOOR, "CLOSED"))
    assert _names(mapper.process(_msg(mapper_mod.DESCRIPTOR_DRIVER_DOOR, "OPEN"))) == [
        mapper_mod.SIG_DOOR_OPEN
    ]
