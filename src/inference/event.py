"""Inferred-event domain model — the single source of truth for a derived event's shape.

Events are stored schemaless (Neon `message` JSONB) so a new event type never needs a
migration — but "stored as a document" does not mean "structureless". This module is the
structure: a typed, self-describing model that the runtime *builds* when it emits a derived
event and that (via a generated schema) the frontend *consumes*. Schemaless at rest, richly
typed in memory.

What it models is the **`message` payload** — the unit that is identical whether the event
arrives over Kafka or is read back out of Neon's JSONB. It deliberately does NOT model the
transport wrapper (`name`/`source_app`/`source_type`/`message`) or the Neon row columns;
those are shaping concerns that stay in the core/adapter.

The model has three parts, kept apart on purpose (see the design discussion):

- **envelope** — the fields every derived event has (id, lineage, entity, time, confidence);
- **capabilities** — optional structured facts an event *may* carry (today: `interval`).
  Presence == the capability. A capability being present commits a consumer to nothing —
  it is a latent affordance, not a behavior. Sniffable structurally (`event.interval`).
- **role** — the *declared* intent: what the event is for / how to treat it. NEVER inferred
  from structure, because structure underdetermines it: `car_trip` and `phone_is_charging`
  are structurally identical (both spans) yet differ here (SPAN vs POINT).

Import-clean: pure Pydantic, no transport/state backend, so the transport-agnostic core
(`inference.runtime.core`) can build it without violating its no-`quixstreams` invariant.
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict, computed_field


class Role(str, Enum):
    """Declared intent — orthogonal to capability. Two events with the same shape can
    differ here, so it is a *choice* recorded on the definition, never sniffed."""

    POINT = "point"    # a point-in-time event (the default)
    SPAN = "span"      # an interval worth rendering as a span (start → end), e.g. car_trip
    HIDDEN = "hidden"  # exists only to feed higher-level events; not surfaced on its own


class Contributor(BaseModel):
    """One source event in the lineage graph (an entry in `derived_from`)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    timestamp: int


class Interval(BaseModel):
    """The *interval capability*: an event that spans time. Its presence on an
    `InferredEvent` is the capability — "this event has a start and an end".

    `duration_seconds` is derived here, once, in the one authoritative place. As a
    `computed_field` it also serializes into the contract, so the stored JSON and the
    generated TS type both carry it — nothing downstream re-derives it (and can't drift
    from it). It is kept self-contained (`ended_at` duplicates the envelope `timestamp`
    for spans) so the capability reads on its own without reaching back into the envelope.
    """

    model_config = ConfigDict(extra="forbid")

    started_at: int
    ended_at: int

    @computed_field
    @property
    def duration_seconds(self) -> int:
        return self.ended_at - self.started_at


class InferredEvent(BaseModel):
    """A derived event's `message` payload — the unit shared across Python and TS.

    Strict (`extra="forbid"`): derived events are wholly minted by the runtime, so their
    shape is closed and worth enforcing. (Raw producer events flow through the same JSONB
    column but stay loosely typed — they are not modeled here.)
    """

    model_config = ConfigDict(extra="forbid")

    # --- envelope (always present) --------------------------------------------
    id: str
    name: str                        # the produced event name (== the definition's name)
    inference_type: str              # the engine *type* that produced it (e.g. "session_window")
    user_id: str                     # the entity the pipeline partitions on
    timestamp: int                   # canonical event-time; for a SPAN this equals interval.ended_at
    confidence_score: float
    derived_from: list[Contributor]

    # --- declared intent ------------------------------------------------------
    role: Role = Role.POINT

    # --- capabilities (present == has the capability) -------------------------
    interval: Interval | None = None
