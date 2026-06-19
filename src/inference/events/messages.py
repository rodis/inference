"""Typed message layer.

`Envelope.message` is resolved to a concrete `MessageBase` subclass via
`MESSAGE_REGISTRY` (keyed by `event_name`), falling back to `OpaqueMessage` for
unregistered event types. Cross-cutting traits are expressed as **capability
mixins** (`GeoLocated`, `Derived`) that a concrete message inherits; the matching
`@runtime_checkable` Protocols exist for type-checking/annotation only.

Dispatch is **nominal**: code asks `isinstance(msg, GeoLocated)` (the mixin), not
the structural Protocol — a structural check would falsely match an
`OpaqueMessage` (extra="allow") that merely happens to carry a `location` key.
"""

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class MessageBase(BaseModel):
    """The data payload inside an Envelope. Registered messages are strict."""

    model_config = ConfigDict(extra="forbid")

    event_name: str
    timestamp: int


class OpaqueMessage(MessageBase):
    """Fallback for event types with no registered class — keeps every field."""

    model_config = ConfigDict(extra="allow")


# --- capabilities: mixin (declares fields) + Protocol (typing only) -----------

class GeoPoint(BaseModel):
    lat: float
    lon: float
    altitude_m: float | None = None


class GeoLocated(BaseModel):
    """A message that can carry coordinates. Optional by construction: declaring
    the capability promises the field exists, not that it's always populated."""

    location: GeoPoint | None = None


@runtime_checkable
class GeoLocatedP(Protocol):
    location: GeoPoint | None


class LineageRef(BaseModel):
    envelope_id: str
    event_name: str
    timestamp: int


class Derived(BaseModel):
    """A message derived from contributing events; carries one-hop lineage."""

    derived_from: list[LineageRef] = []


@runtime_checkable
class DerivedP(Protocol):
    derived_from: list[LineageRef]


# --- registry -----------------------------------------------------------------

MESSAGE_REGISTRY: dict[str, type[MessageBase]] = {}


def register(event_name: str):
    """Class decorator registering a concrete message for an event_name."""

    def _wrap(cls: type[MessageBase]) -> type[MessageBase]:
        MESSAGE_REGISTRY[event_name] = cls
        return cls

    return _wrap


def resolve_message_type(event_name: str) -> type[MessageBase]:
    """The registered class for this event_name, or OpaqueMessage."""
    return MESSAGE_REGISTRY.get(event_name, OpaqueMessage)


# --- concrete messages --------------------------------------------------------
#
# No event registers a strict typed model today. Events are defined as data
# (events/*.yml, ADR 0003), and derived events emit a superset shape
# (event_name, timestamp, confidence_score, occurred_at, sources, evidence,
# derived_from, ...) that a hand-written strict model would reject. So everything
# resolves to the permissive `OpaqueMessage` (extra="allow"). The registry +
# capability mixins above stay as the seam for typed/per-event models when a
# concrete event wants strict validation or a nominal capability — see the
# typed-message open question in doc/adr/0003-dynamic-event-runtime.md.
#
# Example, for when that day comes:
#
#   @register("some_event")
#   class SomeEventMessage(MessageBase, Derived, GeoLocated):
#       confidence_score: float
#       occurred_at: float
