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

The model has two parts, kept apart on purpose (see the design discussion):

- **envelope** — the fields every derived event has (id, lineage, entity, time);
- **capabilities** — optional structured facts an event *may* carry (`interval`, `place`,
  `journey`, `vehicle`).
  Presence == the capability. A capability being present commits a consumer to nothing —
  it is a latent affordance, not a behavior. Sniffable structurally (`event.interval`).

Deliberately absent: **presentation / role** (span vs point vs hidden). That is a *view*
decision — how one consumer chooses to surface an event — not an intrinsic fact about the
event, so it lives in the consumer (the dashboard), never in this data model. A capability
(e.g. `interval`) is data; how to render it is presentation. `car_trip` and
`phone_is_charging` both carry `interval`; only the dashboard decides one is drawn as a span.

Also deliberately absent: a **confidence score** (removed; resolves ADR 0002's open question).
It existed for weighted composition across derivation hops — a derived event carrying how sure
we were, so a downstream engine could discount it. That never happened, because trust ended up
declared *per consumer* instead: a weight map says how much *this* derivation trusts a given
signal, and it should, since the same signal is not equally trustworthy to every consumer (the
direction-ambiguous car lock is worth 6 to one derivation and nothing to another). With trust in
the consumer's config, a scalar riding on the event is redundant — and it was never comparable
anyway: engines emitted unbounded definition-local weight sums, hardcoded 1.0s, and (in one case)
a count of GPS fixes under one name. An engine's score survives where it is meaningful: local to
detection, logged when it fires (`Decision.score`), never part of this model.

Import-clean: pure Pydantic, no transport/state backend, so the transport-agnostic core
(`inference.runtime.core`) can build it without violating its no-`quixstreams` invariant.
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict, computed_field


class Capability(str, Enum):
    """A structured, *derivable* fact an event carries — declared on the definition,
    independent of presentation. The runtime derives the capability's data generically from
    the event's evidence (its contributors), so which capability an event has is a data-model
    decision, never an engine's concern. Today just one; a second becomes a registry of
    name → deriver (mirroring the engine seam)."""

    INTERVAL = "interval"   # spans time — start/end derived from the lineage's extent
    PLACE = "place"         # happened somewhere — centroid derived from the evidence, label matched
    JOURNEY = "journey"     # went from somewhere to somewhere — endpoints, distance and mode
    VEHICLE = "vehicle"     # corroborated by non-locational evidence — e.g. your own car's signals


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


class Place(BaseModel):
    """The *place capability*: an event that happened **somewhere**.

    Two facts of different kinds, deliberately in one capability:

    - `lat`/`lon`/`spread_m` are derived from the event's own evidence — the centroid of the
      contributing fixes and how far the furthest one sits from it. Pure, always available,
      and independent of any configuration: an event at an unlisted place still knows where
      it was. `spread_m` is the evidence's *self-reported* precision (not a GPS accuracy
      claim), so a tight cluster reads as a confident point and a loose one doesn't pretend.
    - `label`/`distance_m` are the match against known places (reference data, see
      `inference.runtime.places`). `label` is None when nothing matched, which is a real and
      useful answer — "40 minutes somewhere at 47.195,8.524" is the raw material for naming
      that place later, not a failure.

    The label is resolved at derive time and therefore *frozen* into the event. Renaming or
    adding a place does not retroactively relabel history — re-deriving from the retained raw
    fixes does (see scripts/backtest.py). That is the deliberate trade: events stay immutable
    facts about what was known when they were minted.
    """

    model_config = ConfigDict(extra="forbid")

    lat: float
    lon: float
    spread_m: float
    label: str | None = None
    distance_m: float | None = None
    # Reference data too, alongside `label`: is this a place worth reporting, or the one you
    # live in? A stay at home is a real fact — derived, persisted, queryable — but it has no
    # natural boundaries (you are there for fourteen hours; `max_gap_seconds` chops the cluster
    # wherever iOS stopped sampling), so the "visit" is a sampling artifact and not news. The
    # flag says what KIND of place it is; whether to draw it is the consumer's call, which is
    # what lets the dashboard offer a "show everyday places" toggle without re-deriving.
    # None when nothing matched — an unlabelled stay makes no claim either way.
    everyday: bool | None = None


class Journey(BaseModel):
    """The *journey capability*: an event that went **from somewhere to somewhere**.

    `place` answers "where did this happen?" with one centroid over all the evidence. For a
    trip that answer is meaningless — the centroid of a 24km drive is a field beside the
    motorway. A journey's geography is two points and what lies between them, which is a
    different fact, not a variant of the same one, so it is its own capability.

    Both endpoints are full `Place`s, so they get labelled against the same reference data a
    stay does: the trip that motivated this engine reads **Home → ENNETSeeKLINIK für
    Kleintiere**. They are single fixes by construction (the settled fix on each side of the
    movement, see `trip_window`), hence `spread_m` 0.0 — one fix has no spread, and claiming
    otherwise would dress a GPS accuracy figure up as evidence precision.

    Two distances, because they answer different questions and a loop separates them: a drive
    out to a shop and back has `straight_line_m` ~0 and `path_m` of 20km, and reporting only
    the first would call it a non-journey.

    `mode` is the stream's own majority motion classification (`driving`/`walking`/…), not an
    inference from speed — the phone already ran that classifier, and it is how a `trip` stays
    generic instead of assuming a car. None when no fix made a claim, which is honest: the
    journey happened, we just can't say how.
    """

    model_config = ConfigDict(extra="forbid")

    origin: Place
    destination: Place
    straight_line_m: float
    path_m: float
    mode: str | None = None


class Vehicle(BaseModel):
    """The *vehicle capability*: a journey **corroborated by evidence that isn't locational**.

    This is the answer to "was this drive in *my* car?" — and it exists because that question
    stopped needing its own event. `car_trip` derives a journey by *pairing* two
    direction-ambiguous boundaries, which is the root of a whole family of defects (a lock
    means "locked or unlocked", so a boundary can land on the wrong side and invert the span).
    Once the span comes from motion instead, those boundaries no longer have to be
    directionally correct — they only have to fall **inside** a journey that is already known.
    A lock at entry rather than exit still proves the car was involved.

    Measured over 25 July - 1 August: of 20 replayed journeys, all 14 in the user's own car
    had at least one boundary strictly inside the span (12 had both), and all 6 in a borrowed
    car had **none**. Perfect separation, with no threshold — which is why the rule is simply
    containment, and why the pad is zero (see `_vehicle`).

    `evidence` names the corroborating events, and is deliberately whatever the data contained
    rather than a fixed vocabulary — the deriver classifies structurally (a source with no
    coordinates is not part of the movement), so this capability never learns a concrete event
    name. `confirmed` marks the stronger case of two distinct corroborating signals.

    **Presence is the claim, and absence asserts nothing.** No fragment is emitted when there
    is no corroboration, rather than `Vehicle(known=False)`, because the peripherals could
    simply have been off — the codebase's standing asymmetry between absence of evidence and
    evidence of absence. A consumer may read "no vehicle capability" as "probably not my car";
    the data model does not say so.
    """

    model_config = ConfigDict(extra="forbid")

    evidence: list[str]
    confirmed: bool


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
    derived_from: list[Contributor]

    # --- capabilities (present == has the capability) -------------------------
    interval: Interval | None = None
    place: Place | None = None
    journey: Journey | None = None
    vehicle: Vehicle | None = None
