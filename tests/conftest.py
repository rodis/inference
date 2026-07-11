"""Shared test fixtures.

The derivation core is transport-agnostic (never imports quixstreams) and drivable over a
plain get/set state port, so everything here is exercised in-memory — no Kafka, no Quix.
"""

import pytest

from inference.runtime.definition import EventDefinition


class DictState:
    """Minimal in-memory `StateStore` (get/set) standing in for Quix `State`."""

    def __init__(self):
        self._d: dict = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value) -> None:
        self._d[key] = value


@pytest.fixture
def state():
    return DictState()


@pytest.fixture
def event():
    """Factory for a raw/derived event record ({name, source_app, source_type, message})."""

    def _make(name, timestamp, *, id="evt", user_id="u", **message_extra):
        msg = {"id": id, "name": name, "user_id": user_id, "timestamp": timestamp}
        msg.update(message_extra)
        return {"name": name, "source_app": "test", "source_type": "http_server", "message": msg}

    return _make


@pytest.fixture
def definition():
    """Factory for an `EventDefinition` (source/sink default to the real topics)."""

    def _make(name, engine, engine_config, *, source_topic="raw_sensors",
              sink_topic="high_level_events", capabilities=()):
        return EventDefinition(
            name=name, engine=engine, engine_config=engine_config,
            source_topic=source_topic, sink_topic=sink_topic, capabilities=list(capabilities),
        )

    return _make
