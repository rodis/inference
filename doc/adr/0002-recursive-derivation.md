# ADR 0002 — Recursive derivation: entailment vs correlation

Status: **Partially realized — correlation-over-derived-events works (one runtime, multi-topic handler); entailment tier + cycle enforcement still design-only**
Date: 2026-06-17 (update: 2026-06-20)

> **Update 2026-06-20:** recursive derivation now runs. `got_into_the_car` derives from a *derived*
> event (`car_door_opened`, on `high_level_events`) + a raw event (`device_connected_to_power`, on
> `raw_sensors`) by simply listing both topics in its definition's `source_topics` — no new abstraction,
> the existing `WeightedWindowEngine` + multi-topic subscribe. The one concrete change needed: derived
> events must be valid pipeline inputs, so `finalize()` now stamps `timestamp = int(occurred_at)` and
> derived events resolve to the permissive `OpaqueMessage` (a strict per-event model rejected the
> emitted superset). The **no-cycles** invariant is documented (the `event_name` gatekeeper prevents
> self-derivation) but not yet statically enforced; the stateless **entailment** tier below remains
> design-only.

> A stub to capture the model and the decision boundary. It builds on
> [`0001-message-shaping-pipeline.md`](0001-message-shaping-pipeline.md) (decide → enrich → emit,
> `derived_from` lineage, the deferred multi-handler runtime). Sections below are **target/exploratory**;
> the recommended interim is deliberately small (see [Decision](#decision-draft)).

---

## Context

ADR 0001 gives every derived event one-hop lineage (`derived_from: list[LineageRef]`) and sketches a
future generic `event_lineage(child_id, parent_id)` edge table for **recursive** queries. That schema
already admits a DAG of derivations. The runtime does not: a worker consumes `raw_sensors` and emits
to `high_level_events`; derived events are not re-ingested as contributors to *further* derivations.

Working through a concrete chain surfaces the real question:

```
carplay_connected                      ⟹  car_engine_started          (single event, deterministic)
car_engine_started + car_lock_changed  →   car_door_opened            (two events, time window)
```

The second step is what `WeightedWindowEngine` exists for. The first is a **1:1 logical
consequence** of a single just-generated event. Routing that through the full stateful-worker
template — a pod, a Kafka consumer group, a Redis-backed window, a cooldown lock — to emit a
deterministic implication is wasteful. That waste is the motivating problem.

### Two kinds of derivation

| | **Entailment** | **Correlation** |
|---|---|---|
| Inputs | one event | N events in a window |
| State | none (pure function) | Redis window + cooldown |
| Confidence | ~1.0 by construction | weighted / probabilistic |
| Nature | definitional ("what the event *means*") | inferential ("evidence accumulated") |
| Today's engine | — | `WeightedWindowEngine` |

There is **no third tier.** "Sub-derived" is not a distinct kind — derivation is recursive, and an
event is derived whether its contributors are raw sensors or themselves derived. "Tier" is just depth
in the lineage DAG.

---

## Key observations

1. **Entailment fits the existing engine protocol with no new abstraction.** `InferenceEngine.decide(payload) -> DerivedDraft | None` ([`engines/protocol.py`](../../src/inference/engines/protocol.py))
   admits a *stateless* implementation: a `StaticEntailmentEngine` whose `decide()` is a pure rule-map
   lookup (`event_name → entailed event_name`), ignoring Redis and the window. It reuses the whole
   enricher chain, `finalize`, lineage, and emit path unchanged. So at the **code** layer there is no
   waste.

2. **The waste is purely deployment granularity** — one-pod-per-worker. A stateless entailment handler
   is the cheapest possible thing to co-run (no backend, synchronous), so this is the concrete forcing
   function for ADR 0001's deferred **multi-handler runtime** open question (one process loading
   per-event-type definitions, spawning a handler per type).

3. **Recursive derivation needs derived events to be first-class inputs.** For an entailed/derived
   event to contribute to a further derivation, some worker must consume `high_level_events` as a
   *source* topic. That closes a loop in the topic graph and raises:
   - **No cycles** — the derivation graph must stay a DAG, or entailment/correlation loop forever. Candidate new invariant.
   - **Confidence composition** — a derived event built from another derived event compounds
     uncertainty across hops; a weighted-window score does not naturally compose. Sharpens ADR 0001's
     open question on the cross-engine core (`confidence_score` vs minimal core).

4. **Raws stay ambiguous; the derived event is the interpretation.** A `car_lock_changed` is a
   transition of unknown polarity. It is never rewritten — the derived `car_door_opened` *asserts* the
   resolved interpretation with the raw as a contributor, and confidence lives on the derived layer.
   Append-only; already where `confidence_score` sits today.

---

## Decision (draft)

Boundary: **entailment is inference, not transport.** Do **not** push entailment rules into
Vector/VRL — that would leak inference semantics into the transport layer and break the
decide-vs-transport separation. Vector mints `envelope_id` and re-wraps; it does not infer.

Recommended interim (deliberately minimal, YAGNI — mirrors the
[`_redis_config_from_env` extraction trigger](../invariants.md)): the decision per entailment rule
turns on **whether the entailed event has more than one consumer**.

- **One consumer / not yet shared → inline.** Fold the entailment into the worker that already
  consumes the source event, as a stateless input-normalization step before the window. Zero new
  infra; knowledge stays local. Do **not** materialize the intermediate event globally yet.
- **Second consumer appears → materialize via one shared stateless expander.** A single
  `StaticEntailmentEngine` driven by a rule *table* (N rules, one deployment) — **not** one pod per
  rule. Emits the entailed events so any worker can consume them like raw events.

When entailment rules proliferate, that is the trigger to actually spec the multi-handler runtime
(its own ADR), at which point entailment handlers co-reside in one process with correlation handlers.

---

## Open questions

- **Cycle prevention** — static validation of the topic/derivation graph, or a runtime hop-count /
  lineage-depth guard? Where does the DAG constraint get enforced?
- **Confidence composition across hops** — does the core stay `confidence_score`, or move to a minimal
  core with engine-specific metrics in `fields` (per ADR 0001's cross-engine-core question)?
- **Materialization default** — is "inline until a second consumer" the right bias, or should entailed
  facts that are *obviously* reusable (e.g. `car_engine_started`) be materialized eagerly?
- **Lineage for inlined entailments** — if an entailment is inlined and never emitted, its
  contribution is invisible to `derived_from` / the future `event_lineage` table. Acceptable, or does
  inlining still need to record provenance?
- **Relationship to the multi-handler runtime** — does this ADR fold into that one, or stay separate
  as the "what kinds of derivation exist" record while the runtime ADR owns "how they're deployed"?

---

## Alternatives considered (sketch)

- **A pod per entailment rule.** Rejected — the motivating waste; a stateful-worker template applied to
  a stateless, deterministic 1:1 mapping.
- **Entailment in Vector/VRL.** Rejected — inference logic in the transport layer; violates separation.
- **A new "sub-derived" event kind / second protocol.** Rejected — derivation is recursive under one
  `decide()` contract; entailment is just a stateless engine. No new abstraction earns its keep.

---

When this moves past draft, update the normative docs
([`architecture.md`](../architecture.md), [`invariants.md`](../invariants.md),
[`classes.md`](../classes.md)) per the "update docs alongside behavior" rule in
[`CLAUDE.md`](../../CLAUDE.md).
