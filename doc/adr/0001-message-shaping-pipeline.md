# ADR 0001 — Message shaping pipeline: decide → enrich → emit

Status: **Accepted — Phases 1, 2a, 2b implemented; Phase 3 (persistence) pending**
Date: 2026-06-14 (2b: 2026-06-15)

> This is a design/decision record describing the **target** architecture and its rationale.
> **Phases 1 (decide → enrich → emit), 2a (`envelope_id`, Vector-minted), and 2b (typed messages +
> protocol-driven enrich applicability) are implemented**; persistence (3) is not, and sections
> describing it (PostGIS, the `events` table) remain target-state. Capability **detection is nominal**
> (`isinstance` on the mixin), and enricher **applicability is a declared `requires` capability**
> checked centrally by the pipeline — see [Phased rollout](#phased-rollout).

---

## Context

We need to persist inference events to Postgres (Neon) for analysis (incl. PostGIS) and we want a
**single, general message structure** rather than a table per event type. Working through "what
should a message look like" surfaced a deeper problem in the worker itself.

Today `WeightedWindowEngine.process()` ([`weighted_window.py`](../../src/inference/engines/weighted_window.py))
does two unrelated jobs at once:

1. **Decides** whether to emit a high-level event — the gatekeeper, the time-window buffer, the
   weighted sum, the threshold check, and the cooldown lock.
2. **Shapes** the entire output message — `inference_type`, `processed_at`, and the whole `message`
   body (`event_name`, `confidence_score`, `occurred_at`, `sources`, `evidence`).

Conflating "should this fire?" with "what does the resulting event look like, in every aspect?" is
the source of the complexity. Every new concern we want on a derived event (coordinates, lineage,
…) would pile more shaping logic into the engine, and would have to be re-done for every future
engine.

### Current state (for reference)

- `InferenceEngine` protocol ([`engines/protocol.py`](../../src/inference/engines/protocol.py)):
  `def process(self, payload: Envelope) -> dict | None`.
- `WeightedWindowEngine.process()` returns, on trigger:
  ```python
  {
      "inference_type": name,
      "processed_at": time.time(),
      "message": {
          "event_name": name,
          "confidence_score": int,
          "occurred_at": float,
          "sources": [event_type, ...],
          "evidence": {event_type: earliest_ts},
      },
  }
  ```
  It buffers contributors in a Redis ZSET (`member = f"{event_name}:{ts}"`, score `ts`), prunes by
  window, dedups by event type keeping the earliest, sums weights, and acquires a `SET NX EX`
  cooldown lock before returning.
- `Envelope` ([`events/envelope.py`](../../src/inference/events/envelope.py)) has five fields:
  `event_name, source_app, source_type, timestamp, message: dict`. **No `envelope_id`.**
- The result dict is POSTed to Vector, which **re-wraps** it into an envelope and publishes to Kafka
  `high_level_events`. So the engine output is intentionally *not* an `Envelope`.
- Vector runs as in-cluster ConfigMaps (namespace `vector`); it has **no** Postgres sink today.
  Neon `aware` is empty, PostGIS 3.5 available but not installed.

---

## Decision

Split the worker's "produce a high-level event" path into three roles with a clean seam between each.

```
Kafka(raw_sensors)
   │
   ▼
KafkaStreamHandler ── envelope ──▶ engine.decide(envelope) ─── DerivedDraft | None
                                                                    │ (None ⇒ commit & move on)
                                                                    ▼
                                          EnrichmentPipeline:  [ LineageEnricher, GeoEnricher, … ]
                                          (ordered fold; each enricher self-decides applicability)
                                                                    │
                                                                    ▼
                                                            finalize(draft) ──▶ dict
                                                                    │
                                                                    ▼
                                                              Emitter.emit(dict) ──▶ Vector ──▶ Kafka(high_level_events)
```

### 1. Engine = decider + core assembler (a swappable Protocol)

The engine **only decides and assembles the core**. The shared, engine-agnostic contract becomes:

```python
class InferenceEngine(Protocol):
    def decide(self, payload: Envelope) -> DerivedDraft | None: ...
```

On trigger it returns a `DerivedDraft` carrying the **core** (`event_name`, `confidence_score`,
`occurred_at`) plus the **contributors** (the source events that triggered it). It no longer builds
`sources`/`evidence`/`location` or any capability-specific shape.

**`WeightedWindowEngine` is one implementation among future others** (e.g. a Bayesian engine). The
gatekeeper, the time window, the weights, the cooldown lock, **and Redis itself** are
`WeightedWindowEngine`-private details — fine to change — and must **not** leak into
`engines/protocol.py` or into `DerivedDraft`. A different engine implements `decide()` and
accumulates its contributors however it likes, with whatever backend. This restates the existing
**Engine-Owned Infrastructure** invariant ([`invariants.md`](../invariants.md)).

### 2. An enricher chain shapes the message

An ordered chain of **enrichers** progressively shapes the draft. Each enricher:

- owns exactly **one capability/protocol**;
- **declares applicability** via a `requires` capability (the mixin a contributor's message must be
  an instance of, or `None` = always). The **pipeline** checks it centrally and only calls `enrich`
  when it applies — the enricher never re-decides whether to run (no `applies()` method, no internal
  apply-guard);
- applicability is judged on the **contributors** (inputs): e.g. `GeoEnricher` requires a `GeoLocated`
  contributor. Contributors aren't geolocated ⇒ the derived event isn't geolocated.

```python
@runtime_checkable
class Enricher(Protocol):
    requires: type | None              # capability a contributor's message must have
    def enrich(self, draft: DerivedDraft) -> DerivedDraft: ...

# pipeline gate: requires is None or any(isinstance(c.message, requires) for c in contributors)
```

Examples:
- **`LineageEnricher`** — always applies; maps `draft.contributors` → `derived_from: list[LineageRef]`.
- **`GeoEnricher`** — applies only if contributors satisfy `GeoLocated`; computes the new event's
  `location` from them; otherwise passes through untouched.

Error handling is **best-effort / skip-on-error**: by the time enrichers run, the decision has fired
and the cooldown lock is taken, so a failing enricher must not drop the whole event — it is skipped
(reported via the observer) and the chain continues with a partially-enriched draft. This mirrors the
handler's existing skip-and-commit philosophy.

### 3. `finalize()` → dict → Emitter (transport unchanged)

`finalize(draft)` merges the core + accreted capability `fields` into the message body, (later)
validates it against the registered typed message model, and returns the transport dict. The
`Emitter.emit(dict)` contract and the "Vector re-wraps the output" behavior are unchanged, so this
refactor is **internal to the worker** and does not alter the Vector/Kafka contract.

### Intermediate type — a neutral `DerivedDraft`, validated once

The chain carries a neutral, frozen builder, **not** a partially-built typed message:

```python
class DerivedDraft(BaseModel):      # frozen; enrichers use model_copy(update=...)
    inference_type: str
    event_name: str
    confidence_score: float           # see open question on cross-engine core
    occurred_at: float
    contributors: tuple[Envelope, ...]  # the contributing source events, in full
    fields: dict = {}                 # capability output accretes here (location, derived_from, …)
```

A contributor *is* a source event, which in this system is an `Envelope`. Carrying the full
envelope (rather than a flattened `{event_name, timestamp, message}` struct) avoids duplicating the
message's own fields, gives enrichers complete context (message body for geo, source metadata), and
means `envelope_id` flows into lineage automatically once Vector mints it in Phase 2 — no extra
plumbing. The engine persists the full envelope in its window store and reconstructs it on trigger.

Rationale: a partially-built *typed* model is always invalid mid-chain (fights pydantic's
validate-on-construction). A neutral draft lets enrichers accrete `fields` freely; validation against
the typed message happens **once**, at `finalize()`.

### Capability model — mixins + runtime-checkable Protocols

Capabilities are a hybrid: a Pydantic **mixin** declares the fields, and a matching
`@runtime_checkable` **Protocol** enables `isinstance` dispatch in generic code (enrichers).

- **`GeoLocated`** — canonical `location: GeoPoint{lat, lon, altitude_m?}` (one canonical path so the
  future DB layer can promote/index it).
- **`Derived`** — `derived_from: list[LineageRef{envelope_id, event_name, timestamp}]`.

A `MESSAGE_REGISTRY[event_name] -> type[MessageBase]` resolves the concrete message class, with an
`OpaqueMessage` (`extra="allow"`) fallback so **new event types need no code**.

### The contributor-data consequence (important)

Enrichers shape the new event **from the contributors' data** (e.g. their coordinates). Therefore the
engine must supply **full contributor bodies** in the `DerivedDraft`, not just type + timestamp.

- The **contract** is engine-agnostic: "the engine returns contributors with enough data for
  enrichers."
- **How** an engine retains them is engine-private. For `WeightedWindowEngine` specifically, the ZSET
  (which only stores `event:ts`) is not enough; it will keep full bodies in a parallel Redis **HASH**
  keyed alongside the ZSET, pruned to the same window (diff-prune against surviving members). This is
  a WeightedWindow detail, **not** a Protocol requirement.

### `envelope_id` — IMPLEMENTED

Stable lineage needs stable IDs. `envelope_id` is **minted by Vector at ingest** — the
`classify_domain` transform sets `.envelope_id = uuid_v4()` (if absent) for every event, so every
consumer shares one authoritative ID. The `Envelope` model carries it with a
`Field(default_factory=uuid4)` fallback so an event without one still parses (a worker-minted id is
stable within one process but not across consumers — Vector's is authoritative). `LineageEnricher`
emits the contributors' real `envelope_id`s in `derived_from`.

(`uuid_v4`, not `uuid_v7`: v4 is guaranteed available in the deployed Vector and fully satisfies the
identity/lineage need. Switching to `uuid_v7` for time-ordered Postgres primary keys is a Phase-3
consideration, gated on confirming VRL support; the DB side `pg_uuidv7` is already available.)

### Wiring

Per-worker, the enricher chain is an ordered list in `main.py` next to `RULES`, keeping `main.py`
pure wiring:

```python
RULES = { "name": WORKER_NAME, "threshold": 10, "window_seconds": 600, ... }
ENRICHERS = [ LineageEnricher(), GeoEnricher(strategy="centroid") ]   # order = chain order

engine   = WeightedWindowEngine(rules=RULES)
pipeline = EnrichmentPipeline(enrichers=ENRICHERS)
KafkaStreamHandler(kafka_consumer=…, engine=engine, observer=…, emitter=…, pipeline=pipeline)
```

The `KafkaStreamHandler` owns the call sequence engine → pipeline → emitter; `main.py` builds the
pipeline.

---

## Consequences

- The engine shrinks to a decision-maker; adding a derived-event concern (geo, lineage, …) is a new
  **enricher**, not an engine edit — and it's shared across all engine implementations.
- A new `InferenceEngine` (Bayesian, …) only has to produce a `DerivedDraft`; it reuses the entire
  enricher chain, `finalize`, emitter, and transport unchanged.
- `WeightedWindowEngine` must persist contributor bodies (new Redis HASH + prune step) — bounded by
  the window, engine-private.
- New cross-cutting structure (capabilities) plus one general message shape enables a **single**
  Postgres `events` table later, with no table per type.
- The Vector/Kafka contract is untouched in Phase 1 (engine still yields a dict; Vector still
  re-wraps).

---

## Future persistence (sketch — not built here)

- One general **`events`** table: promoted/indexed columns (`envelope_id`, `event_name`,
  `source_app`, `source_type`, `occurred_at`, `ingested_at`) + `payload jsonb` for type-specific
  fields + a PostGIS **generated** `geom geometry(Point,4326)` column derived from the canonical
  `payload.location` path (GiST-indexed, NULL when absent).
- A **generic** `event_lineage(child_id, parent_id)` **edge table** (one table for all types — not
  per-type), populated by a DB trigger that expands `payload.derived_from`. Enables bidirectional and
  recursive lineage queries.
- **Write path = Vector's native `postgres` sink** (Beta; maps JSON → row via
  `jsonb_populate_recordset`), fed by a Vector `kafka` source over both topics, not a custom worker.
  Caveat to design around: no cross-batch transactions and a PK conflict fails the whole batch under
  Kafka at-least-once redelivery → needs a dedup/idempotency strategy (e.g. an `INSERT … ON CONFLICT
  DO NOTHING` via a staging rule). A small Python writer remains the fallback if the sink's semantics
  prove too coarse.
- `CREATE EXTENSION postgis;` required before the `events` DDL. TimescaleDB available but not adopted
  (revisit only at high volume).

---

## Alternatives considered

- **Keep shaping in the engine.** Rejected — it's the status quo that conflates decision and shaping,
  doesn't compose across capabilities, and must be re-implemented per engine.
- **Carry a typed `MessageBase` through the chain.** Rejected — a partially-built typed model is
  invalid mid-chain; either everything becomes `Optional` (erasing the type guarantees) or we bypass
  validation. The neutral `DerivedDraft` + validate-once-at-finalize is cleaner.
- **Enricher `applies()` + `enrich()` split.** Rejected — keeps the runner dumb and avoids an
  ordering coupling; applicability is internal (return-unchanged).
- **Per-type tables / writer-supplied geometry.** Rejected — a single JSONB table + generated geom
  column keeps the "no table per type" goal and the geo contract in the database.
- **Custom Python Kafka→Postgres writer as the primary write path.** Deprioritized — Vector's native
  `postgres` sink covers it; the writer is a fallback.

---

## Open questions

- **GeoEnricher applicability:** currently `any` contributor `GeoLocated` (centroid over the located
  subset). `all`, or require the *triggering* contributor, still open.
- **Location strategy:** centroid vs latest/triggering point vs highest-weight contributor — a
  `GeoEnricher(strategy=…)` knob; only `centroid` implemented, default deferred until a geolocated producer exists.
- **`sources`/`evidence` fate:** replace with `derived_from`, or keep transitionally for any existing
  `high_level_events` consumer (none consume it today). Currently kept (superset).
- **`processed_at` determinism:** wall-clock vs derived-from-envelope, if replay-equality matters.
- **Worker facade:** should a `Worker` object own engine+pipeline+emitter instead of the handler.
- **Event-definition / multi-handler runtime (future direction):** one app loading per-event-type
  definitions (YAML) and spawning a handler per type, collapsing one-pod-per-event. Larger change
  (concurrency, config schema, deploy/CI, worker-identity) — its own ADR when pursued.

**Resolved:** capability detection is **nominal** (`isinstance` on the mixin, not the structural
Protocol — `OpaqueMessage(extra="allow")` false-matches structurally). Enricher applicability is a
declared `requires` checked centrally (not self-decided). Engine method renamed `process` → `decide`.
- **Cross-engine core:** is `confidence_score` the right shared `DerivedDraft` field, or should the
  core be minimal (`event_name`, `occurred_at`, `contributors`) with engine-specific metrics (weighted
  score, Bayesian posterior) living in `fields`?

---

## Phased rollout

1. **Phase 1 — enricher pipeline refactor. ✅ IMPLEMENTED.** `engine.decide() -> DerivedDraft`,
   `EnrichmentPipeline`, `LineageEnricher`, `GeoEnricher` scaffold (no-op until geolocated data exists),
   `WeightedWindowEngine` contributor persistence (Redis HASH), `ENRICHERS` wiring in `main.py`. Message
   stays a dict / duck-typed; the emitted payload is a superset of the prior output (adds `derived_from`),
   so it stays Vector-compatible. No external dependencies.
2. **Phase 2 — identity + typed message layer.**
   - *2a — `envelope_id`. ✅ IMPLEMENTED.* Vector's `enrich_sensor` transform mints `uuid_v4()` at
     ingest (sensor traffic only, post-routing); `Envelope.envelope_id` (with `default_factory`
     fallback); `LineageEnricher` emits real ids. (Vector config in helm-override-files.)
   - *2b — typed messages + protocol-driven applicability. ✅ IMPLEMENTED.* `MessageBase`/`OpaqueMessage`
     /`MESSAGE_REGISTRY`, `GeoLocated`/`Derived` mixins (+ `*P` Protocols for typing only); `Envelope.message`
     is now `SerializeAsAny[MessageBase]` resolved via the registry. Enrichers declare a `requires`
     capability; the pipeline applies each only when a contributor's message satisfies it
     (**nominal** `isinstance` on the mixin — structural Protocol rejected: `OpaqueMessage(extra="allow")`
     would false-match). `finalize` still emits a superset dict (no strict typed-output validation yet).
     Kept on the current one-pod-per-worker model (the event-definition/YAML multi-handler runtime is a
     separate future direction, out of scope).
3. **Phase 3 — persistence.** `CREATE EXTENSION postgis;` + `events` table (generated `geom`) +
   generic `event_lineage` edge table; Vector `kafka` source → `postgres` sink. Switch the engine to
   emit a full typed `Envelope` and make Vector pass-through.

When each phase lands, update the normative docs ([`architecture.md`](../architecture.md),
[`invariants.md`](../invariants.md), [`classes.md`](../classes.md)) to match — per the
"update docs alongside behavior" rule in [`CLAUDE.md`](../../CLAUDE.md).
