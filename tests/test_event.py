"""InferredEvent domain model: computed duration, serialization, strictness."""

import pytest
from pydantic import ValidationError

from inference.event import Contributor, InferredEvent, Interval


def _evt(**over):
    base = dict(
        id="i", name="car_trip", inference_type="session_window",
        user_id="u", timestamp=10, confidence_score=1.0, derived_from=[],
    )
    base.update(over)
    return InferredEvent(**base)


def test_interval_duration_is_computed():
    assert Interval(started_at=100, ended_at=250).duration_seconds == 150


def test_interval_duration_serializes():
    dumped = Interval(started_at=100, ended_at=250).model_dump(mode="json")
    assert dumped == {"started_at": 100, "ended_at": 250, "duration_seconds": 150}


def test_interval_optional_defaults_none():
    assert _evt().model_dump(mode="json")["interval"] is None


def test_interval_present_serializes_nested_with_duration():
    dumped = _evt(interval=Interval(started_at=1, ended_at=5)).model_dump(mode="json")
    assert dumped["interval"] == {"started_at": 1, "ended_at": 5, "duration_seconds": 4}


def test_derived_from_is_projected_shape():
    e = _evt(derived_from=[Contributor(id="a", name="x", timestamp=1)])
    assert e.model_dump(mode="json")["derived_from"] == [{"id": "a", "name": "x", "timestamp": 1}]


def test_extra_fields_forbidden():
    # `role` was removed from the data model — an unknown field must be rejected, not silently kept.
    with pytest.raises(ValidationError):
        _evt(role="span")
