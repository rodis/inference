"""Capability derivers: interval derives generically from the source events' extent."""

import pytest

from inference.event import Capability
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
