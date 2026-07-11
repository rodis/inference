/* AUTO-GENERATED from contracts/inferred_event.schema.json — do not edit. Regenerate: uv run python scripts/emit_event_schema.py && npm run gen:types */

export type Id = string;
export type Name = string;
export type InferenceType = string;
export type UserId = string;
export type Timestamp = number;
export type ConfidenceScore = number;
export type Id1 = string;
export type Name1 = string;
export type Timestamp1 = number;
export type DerivedFrom = Contributor[];
export type StartedAt = number;
export type EndedAt = number;
export type DurationSeconds = number;

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
  confidence_score: ConfidenceScore;
  derived_from: DerivedFrom;
  interval?: Interval | null;
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
