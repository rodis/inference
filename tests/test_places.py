"""`PlaceBookRefresher` — reference-data freshness without a background thread.

The design constraint these cover: the runtime has no liveness probe, so a dead refresher
thread would be invisible. Refreshing on the event stream instead means any failure is both
visible and non-fatal — the previous book survives.
"""

import itertools

from inference.runtime.places import PlaceBookRefresher

_A = [{"name": "A", "lat": 1.0, "lon": 2.0, "radius_m": 50}]
_B = [{"name": "B", "lat": 3.0, "lon": 4.0, "radius_m": 50}]


class _Clock:
    """Monotonic stand-in so the tests never sleep."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _refresher(ttl, books, clock, installed):
    it = iter(books)
    return PlaceBookRefresher("dsn", ttl, loader=lambda _dsn: next(it),
                              setter=installed.append, clock=clock)


def test_tick_passes_the_value_through_untouched():
    installed, clock = [], _Clock()
    r = _refresher(60, [_A], clock, installed)
    sentinel = {"message": {"name": "location_ping"}}
    assert r.tick(sentinel) is sentinel


def test_no_reload_before_the_ttl_expires():
    installed, clock = [], _Clock()
    r = _refresher(60, [_A], clock, installed)
    clock.t += 59
    r.tick("x")
    assert installed == []


def test_reload_once_the_ttl_expires():
    installed, clock = [], _Clock()
    r = _refresher(60, [_A], clock, installed)
    clock.t += 61
    r.tick("x")
    assert installed == [_A]


def test_ttl_zero_disables_refreshing():
    """The pre-2026-08-01 load-once behaviour stays available as an escape hatch."""
    installed, clock = [], _Clock()
    r = _refresher(0, [_A], clock, installed)
    clock.t += 100_000
    r.tick("x")
    assert installed == []


def test_a_failing_reload_keeps_the_previous_book_and_does_not_raise():
    """Degraded mode, not an outage: a Neon blip must not take the pipeline down, and must not
    wipe the labels we already had."""
    installed, clock = [], _Clock()
    def boom(_dsn):
        raise RuntimeError("neon unreachable")
    r = PlaceBookRefresher("dsn", 60, loader=boom, setter=installed.append, clock=clock)
    clock.t += 61
    assert r.tick("x") == "x"        # value still passes through
    assert installed == []           # nothing installed, so the old book survives


def test_a_failing_reload_does_not_retry_on_every_event():
    """The subtle one. A location stream delivers ~1 fix per 11s; if a failed reload left the
    timestamp unstamped, one Neon outage would become a connection storm."""
    installed, clock, calls = [], _Clock(), itertools.count()
    def boom(_dsn):
        next(calls)
        raise RuntimeError("neon unreachable")
    r = PlaceBookRefresher("dsn", 60, loader=boom, setter=installed.append, clock=clock)
    clock.t += 61
    for _ in range(50):              # 50 events arriving inside one TTL window
        r.tick("x")
    assert next(calls) == 1, "the failed reload must not retry until the next TTL"
