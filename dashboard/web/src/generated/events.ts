/* AUTO-GENERATED from contracts/inferred_event.schema.json — do not edit. Regenerate: uv run python scripts/emit_event_schema.py && npm run gen:types */

export type Id = string;
export type Name = string;
export type InferenceType = string;
export type UserId = string;
export type Timestamp = number;
export type Id1 = string;
export type Name1 = string;
export type Timestamp1 = number;
export type DerivedFrom = Contributor[];
export type StartedAt = number;
export type EndedAt = number;
export type DurationSeconds = number;
export type Lat = number;
export type Lon = number;
export type SpreadM = number;
export type Label = string | null;
export type DistanceM = number | null;
export type Everyday = boolean | null;
export type StraightLineM = number;
export type PathM = number;
export type Mode = string | null;
export type Evidence = string[];
export type Confirmed = boolean;
export type Level = string;
export type EvidenceKinds = string[];
export type Pauses = Pause[] | null;
export type StartedAt1 = number;
export type EndedAt1 = number;
export type DurationSeconds1 = number;

/**
 * A derived event's `message` payload — the unit shared across Python and TS.
 *
 * Strict (`extra="forbid"`): derived events are wholly minted by the runtime, so their
 * shape is closed and worth enforcing. (Raw producer events flow through the same JSONB
 * column but stay loosely typed — they are not modeled here.)
 */
export interface InferredEvent {
  id: Id;
  name: Name;
  inference_type: InferenceType;
  user_id: UserId;
  timestamp: Timestamp;
  derived_from: DerivedFrom;
  interval?: Interval | null;
  place?: Place | null;
  journey?: Journey | null;
  vehicle?: Vehicle | null;
  support?: Support | null;
  pauses?: Pauses;
}
/**
 * One source event in the lineage graph (an entry in `derived_from`).
 */
export interface Contributor {
  id: Id1;
  name: Name1;
  timestamp: Timestamp1;
}
/**
 * The *interval capability*: an event that spans time. Its presence on an
 * `InferredEvent` is the capability — "this event has a start and an end".
 *
 * `duration_seconds` is derived here, once, in the one authoritative place. As a
 * `computed_field` it also serializes into the contract, so the stored JSON and the
 * generated TS type both carry it — nothing downstream re-derives it (and can't drift
 * from it). It is kept self-contained (`ended_at` duplicates the envelope `timestamp`
 * for spans) so the capability reads on its own without reaching back into the envelope.
 */
export interface Interval {
  started_at: StartedAt;
  ended_at: EndedAt;
  duration_seconds: DurationSeconds;
}
/**
 * The *place capability*: an event that happened **somewhere**.
 *
 * Two facts of different kinds, deliberately in one capability:
 *
 * - `lat`/`lon`/`spread_m` are derived from the event's own evidence — the centroid of the
 *   contributing fixes and how far the furthest one sits from it. Pure, always available,
 *   and independent of any configuration: an event at an unlisted place still knows where
 *   it was. `spread_m` is the evidence's *self-reported* precision (not a GPS accuracy
 *   claim), so a tight cluster reads as a confident point and a loose one doesn't pretend.
 * - `label`/`distance_m` are the match against known places (reference data, see
 *   `inference.runtime.places`). `label` is None when nothing matched, which is a real and
 *   useful answer — "40 minutes somewhere at 47.195,8.524" is the raw material for naming
 *   that place later, not a failure.
 *
 * The label is resolved at derive time and therefore *frozen* into the event. Renaming or
 * adding a place does not retroactively relabel history — re-deriving from the retained raw
 * fixes does (see scripts/backtest.py). That is the deliberate trade: events stay immutable
 * facts about what was known when they were minted.
 */
export interface Place {
  lat: Lat;
  lon: Lon;
  spread_m: SpreadM;
  label?: Label;
  distance_m?: DistanceM;
  everyday?: Everyday;
}
/**
 * The *journey capability*: an event that went **from somewhere to somewhere**.
 *
 * `place` answers "where did this happen?" with one centroid over all the evidence. For a
 * trip that answer is meaningless — the centroid of a 24km drive is a field beside the
 * motorway. A journey's geography is two points and what lies between them, which is a
 * different fact, not a variant of the same one, so it is its own capability.
 *
 * Both endpoints are full `Place`s, so they get labelled against the same reference data a
 * stay does: the trip that motivated this engine reads **Home → ENNETSeeKLINIK für
 * Kleintiere**. They are single fixes by construction (the settled fix on each side of the
 * movement, see `trip_window`), hence `spread_m` 0.0 — one fix has no spread, and claiming
 * otherwise would dress a GPS accuracy figure up as evidence precision.
 *
 * Two distances, because they answer different questions and a loop separates them: a drive
 * out to a shop and back has `straight_line_m` ~0 and `path_m` of 20km, and reporting only
 * the first would call it a non-journey.
 *
 * `mode` is the stream's own majority motion classification (`driving`/`walking`/…), not an
 * inference from speed — the phone already ran that classifier, and it is how a `trip` stays
 * generic instead of assuming a car. None when no fix made a claim, which is honest: the
 * journey happened, we just can't say how.
 */
export interface Journey {
  origin: Place;
  destination: Place;
  straight_line_m: StraightLineM;
  path_m: PathM;
  mode?: Mode;
}
/**
 * The *vehicle capability*: a journey **corroborated by evidence that isn't locational**.
 *
 * This is the answer to "was this drive in *my* car?" — and it exists because that question
 * stopped needing its own event. `car_trip` derives a journey by *pairing* two
 * direction-ambiguous boundaries, which is the root of a whole family of defects (a lock
 * means "locked or unlocked", so a boundary can land on the wrong side and invert the span).
 * Once the span comes from motion instead, those boundaries no longer have to be
 * directionally correct — they only have to fall **inside** a journey that is already known.
 * A lock at entry rather than exit still proves the car was involved.
 *
 * Containment is the engine's decision, not this model's: a boundary counts inside the span
 * plus a small pad (a correctly-measured journey systematically excludes both boundaries),
 * stretched across the adjacent evidence gap when the engine is configured gap-tolerant
 * (issue #46 — a cold-start entry or parking-search exit falls minutes outside any pad that
 * stays phantom-free). Measured 2026-08-08 over 25 days: 19 of 27 own-car journeys
 * `confirmed`, none absent, borrowed-car legs clean.
 *
 * `evidence` names the corroborating events, and is deliberately whatever the data contained
 * rather than a fixed vocabulary — the deriver classifies structurally (a source with no
 * coordinates is not part of the movement), so this capability never learns a concrete event
 * name. `confirmed` marks the stronger case of two distinct corroborating signals.
 *
 * **Presence is the claim, and absence asserts nothing.** No fragment is emitted when there
 * is no corroboration, rather than `Vehicle(known=False)`, because the peripherals could
 * simply have been off — the codebase's standing asymmetry between absence of evidence and
 * evidence of absence. A consumer may read "no vehicle capability" as "probably not my car";
 * the data model does not say so.
 */
export interface Vehicle {
  evidence: Evidence;
  confirmed: Confirmed;
}
/**
 * The *support capability*: **what kind of evidence backs this claim** (ADR 0011).
 *
 * This is the shape in which the removed `confidence_score` returns — deliberately not a
 * scalar. A probability cannot be calibrated at this system's scale (one user, ~22 labelled
 * trips killed `naive_bayes_window`), and the dominant failure mode is ambiguity, which is
 * invariant to scoring: identical evidence gets identical numbers whether the event is real
 * or phantom (ADR 0009). What *is* honestly knowable is the **topology** of the evidence —
 * how many independent kinds of it agree — and that is all this model states.
 *
 * `evidence_kinds` lists the independent kinds structurally: `"geometry"` when located fixes
 * are among the evidence, plus the name of each corroborating *claim* (a derived event that
 * contributed as evidence rather than as a constituent of another). Kinds are whatever the
 * data contained, never a fixed vocabulary — a future transit or bicycle detector slots in
 * without touching this model.
 *
 * `level` is the one-word summary consumers threshold on: `corroborated` when two or more
 * independent kinds agree, `single_source` otherwise. A `single_source` journey is still a
 * real claim (200 fixes of geometry are solid evidence of *movement*) — the grade says how
 * the claim would survive one source being wrong, not how firmly it is believed. Consumers
 * own their cut: a timeline may render `single_source` paler; an action lane may require
 * `corroborated`.
 */
export interface Support {
  level: Level;
  evidence_kinds: EvidenceKinds;
}
/**
 * One *pause* inside a journey: the entity held still, but not long enough to be an
 * arrival. Detection thresholds are deliberately not lowered to see these — a 2026-08-08
 * fuel stop (3m46s at Avia Neuheim, under `settle_seconds` 300) correctly did not split the
 * journey, because *no* threshold separates a fuel stop from a rail crossing, and an engine
 * that tries mints phantom micro-stays at every long red light. So the stop is **detail the
 * journey carries**, derived from the evidence the journey already retains, rather than an
 * event of its own: detection guards against phantoms, enrichment carries nuance, and the
 * reading "one errand with a stop at the station" and "you stopped somewhere" are both true
 * at their own altitude. Labelled against the same place book as everything else, so a pause
 * at a known place says so ("~4 min at Avia Neuheim") and one at a red light stays anonymous
 * coordinates — which is honest, and no one has to declare red lights in advance.
 */
export interface Pause {
  started_at: StartedAt1;
  ended_at: EndedAt1;
  place: Place;
  duration_seconds: DurationSeconds1;
}
