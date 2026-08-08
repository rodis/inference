# Invariants

Design rules that must hold across the codebase. This is the **normative checklist** — short,
checkable claims with the reason attached. The explanation of how any of it works lives in
[`core.md`](core.md); the reasoning behind the architecture lives in the [ADRs](adr/).

Use this when reviewing a change to `src/`. If a rule here is wrong, fix the rule in the same commit
as the code — a stale invariant is worse than no invariant.

> Rewritten 2026-07-27 for the Quix runtime. The pre-Quix rules (Redis-key identity, engine-owned
> Redis, transport adapters, single-writer-via-Lua, the ordered enricher chain, per-event consumer
> groups) are gone with the code they governed — read them in git history alongside
> `architecture.md` and `classes.md`.

---

## 1. The core is import-clean

**[`runtime/core.py`](../src/inference/runtime/core.py) and everything it imports must not import
`quixstreams`, or any other transport or state backend.**

*Why:* it makes the derivation graph portable and drivable without a broker. This is not
aspirational — five adapters already depend on it (production, the test suite, `backtest.py`,
`trip_eval.py`, `rederive.py`), and it is what lets a weight-map change be evaluated against real
history before it ships.

*How to apply:* transport and database access live in `runtime/quix.py` and
`runtime/places.py` only. In the latter two, `import psycopg` goes **inside the function**, so the
in-memory paths need no driver present. CI enforces the rule by installing the package with
`pip install -e . --no-deps` — a stray transport import fails the build.

## 2. `message` is data; everything outside it is metadata

**Engines and consumers read event data from `message`. The wrapper (`name`, `source_app`,
`source_type`) is routing and provenance metadata only.**

*Why:* one envelope shape across every topic, whether the event was minted by Vector at ingest or by
the runtime's `Shaper`. A consumer parses one shape.

*How to apply:* read `message.name` and `message.timestamp`, never the wrapper's. `source_type` is
metadata that the persister drops — nothing may depend on it reaching Neon.

## 3. Identity is the definition `name`

**A definition's `name` is the emitted `message.name`, the sink-routing key, and the prefix its
per-entity state is scoped under. The emitted `message.inference_type` is the *engine type*, not the
name.**

*Why:* one string is the source of truth for identity, so routing, state and output can never
disagree about what an event is.

*How to apply:* `snake_case`, matching the filename. Never derive identity from a directory name, a
consumer group, or an engine class.

## 4. There is exactly one event-time

**`message.timestamp` is the only event-time. "When the system handled it" is the DB-set
`ingested_at` column.**

*Why:* two timestamps that mean almost the same thing get used interchangeably and then diverge.

*How to apply:* an engine's `Decision.occurred_at` must be the moment the pattern *completed* — the
latest contributing signal, or the last fix inside a cluster. This keeps lineage **monotonic**: a
derived event's timestamp is ≥ every contributor's. Never stamp a derived event earlier than its own
evidence.

## 5. Entity key = partition key = state-ownership unit

**`Router.key_for` (today: `message.user_id`) is simultaneously the window aggregation unit, the
Kafka partition key, and the state key.**

*Why:* co-location makes single-writer-per-key **structural**. No lock, no atomic script, no Redis —
partition ownership is the guarantee (ADR 0004).

*How to apply:* the adapter shards with `group_by(router.key_for)` and nothing else. A missing key
buckets under the explicit `_no_user_id` sentinel with a warning — **never** a plausible-looking
fallback like `source_app`, which would silently fragment one entity's state and, once multi-user,
merge different people.

## 6. Exactly one external source topic

**The runtime consumes one external topic; recursion is resolved in-process.**

*Why:* Quix `concat()` of multiple sources with `auto_offset_reset=latest` consumes zero messages
(bisected in-cluster, ADR 0004). In-process recursion is also lower-latency and cheaper in topics.

*How to apply:* `RoutingPlan.from_definitions` raises if `declared_sources - sink_topics` isn't
exactly one. A genuinely separate feed is merged at **ingest**, in Vector, not by adding a source.

## 7. One shared keyed router, definitions as data

**All definitions run through one `Router` on one pipeline in one consumer group — not a branch,
consumer, or process per definition.**

*Why:* the Aiven free tier caps user topics at 5. A per-definition branch mints a changelog *and* a
repartition topic each; the shared router costs 1 repartition + 1 changelog regardless of definition
count.

*How to apply:* adding an event is a YAML file. If you find yourself adding a stateful operator or a
second `group_by`, count the topics it will mint first.

## 8. Detection and shaping are separate stages

**`Router.route` decides *that* an event fires and mints its identity. `Shaper.shape` decides what
data it carries. Neither does the other's job.**

*Why:* it lets the inference logic and the data model evolve independently — adding the `place`
capability changed no engine and no routing code.

*How to apply:* `route` must not touch lineage, capabilities, or the wrapper. `shape` must not touch
state, engines, or routing. An engine returns a `Decision` and shapes nothing.

## 9. Recursion carries the clean envelope

**The event re-enqueued for in-process recursion is the bare envelope. The `sources` sidecar goes to
the `Shaper` only.**

*Why:* engines store the events they consume in window state. If a recursed event carried its
sources, each hop would nest the previous hop's full bodies — state and changelog would fatten
geometrically down a chain.

*How to apply:* a derived event, as seen by a downstream engine, carries only `id`, `name`,
`inference_type`, `user_id`, `timestamp`. **No engine or capability deriver may depend on an upstream
derived event's lineage or capabilities** — those exist only on the record produced to Kafka.

## 10. The derivation graph is a DAG

**No definition may consume what it produces, directly or transitively.**

*Why:* recursion walks a queue with no cycle detection. The guarantee comes from the definitions.

*How to apply:* a name absent from the consumers index is terminal and stops the cascade. Check the
graph in [`core.md` §5](core.md#5-recursion-without-kafka) before wiring a new derived contributor.

## 11. The engine parses its own config

**The runtime never knows an `engine_config` schema.**

*Why:* it is what makes a strategy swappable — a new engine is a class plus a registry decorator,
with no change to the runtime.

*How to apply:* parse and default `engine_config` in the engine's `__init__`. Framework code
(`core.py`, `quix.py`, `definition.py`) must never name a concrete engine or one of its config keys.

## 12. Engines carry full source bodies; the shaper projects them

**`Decision.sources` holds the whole source event records. `derived_from` is a projection of them;
capabilities are derived from them.**

*Why:* a capability deriver needs message fields the lineage projection doesn't carry (`place` needs
`lat`/`lon`; a future `amount` will need `amount`). Keeping full bodies in the `Decision` is what
lets the data-model layer own derivation with zero engine coupling.

*How to apply:* stash the full event dict in state, not a trimmed subset. Values must be
JSON-serializable — Quix `State` round-trips through the changelog.

## 13. Capabilities scale by addition

**A capability is a registered pure function `(sources) -> fragment of InferredEvent fields`. Adding
one changes no engine, no router, and no shaper.**

*Why:* the enricher chain of ADR 0001 without the ordering. Presence of the field *is* the
capability.

*How to apply:* enum member + model + `InferredEvent` field + `@register_capability` deriver +
`capabilities:` in a definition + regenerate the contract. `Shaper` must never name a capability; it
runs whatever the definition declared. A deriver that finds nothing to say returns `{}` — it must
never fabricate data.

## 14. Reference data is injected by the composition root

**The core never reads a database. `build_runtime` loads reference data and hands it in.**

*Why:* it is what keeps rule 1 true while still letting places and regions be editable data rather
than code.

*How to apply:* load in a `runtime/` module with lazy psycopg, install with an explicit `set_*`
function (see `set_place_book`), keep the consumer a pure function of (evidence, reference data).
Both reads are **best-effort**: a Neon blip degrades to "no region events" or "no labels", never a
crash.

## 15. Presentation is not in the data model

**`InferredEvent` carries no `role`, no span/point/hidden, no colour, no label priority.**

*Why:* how to surface an event is one consumer's view decision, not a fact about the event.
`car_trip` and `stay` both carry `interval`; only the dashboard decides how each is drawn.

*How to apply:* if a field answers "how should this look?", it belongs in the dashboard. If it
answers "what happened?", it belongs here. Reference data *about a place* (`everyday`) is a fact;
whether to draw it is not.

## 16. No cross-hop confidence scalar

**An engine's `score` is detection-local: logged when it fires, never emitted.**

*Why:* trust is declared per consumer, in the weight map — the same signal is not equally
trustworthy to every derivation. A scalar riding on the event was redundant *and* never comparable
across engines (see [`event.py`](../src/inference/event.py)).

*How to apply:* don't reintroduce `confidence_score`. If a downstream derivation needs to discount a
contributor, that is a weight in *its* map.

## 17. Configuration lives at exactly one altitude

| Kind | Home |
|---|---|
| Infra / env (broker, group, state dir, DSN) | `runtime/config.py`, read lazily |
| Per-event logic (engine, thresholds, weights, capabilities) | `events/<name>.yml` |
| Engine-internal defaults | the engine's `__init__` |
| Places and regions | Neon `regions` rows |

*Why:* forcing per-event or engine-internal knowledge through shared config creates a leaky
abstraction where the runtime has to know each engine's internals.

## 18. Configuration errors are fatal; data errors degrade; event errors isolate

**Anything wrong with the declared configuration fails at startup, before traffic. Anything wrong
with external reference data degrades. Anything wrong with a single event is contained.**

*Why:* a fleet that silently isn't doing what you declared is worse than one that refuses to start.

*How to apply:* unknown engine, no definitions, wrong source-topic count →
raise. Neon unreachable → log and continue. Malformed YAML → skip that definition. Bad event → return
`None`/`[]`. See the full table in [`core.md` §14](core.md#14-failure-modes).

## 19. Derived events are a cache; raw signals are the truth

**Anything derived can be rebuilt from retained raws. Nothing derived is a system of record.**

*Why:* a stream processor derives forward only, and some facts are frozen at mint time (a `place`
label). Without replay, a definition change can only ever improve the future.

*How to apply:* keep raw events. Change a definition, then replay: `backtest.py` for *what* changed,
`trip_eval.py` for whether it got *better*, `rederive.py` to rebuild history. Never tune a weight map
on a count delta.

## 20. State is ephemeral by design

**Per-entity state is partition-local RocksDB on an `emptyDir`, recovered from the Kafka changelog.**

*Why:* K8s is elastic disposable compute; everything durable lives in Aiven or Neon. It also means a
restart is cheap, which is what makes shipping a definition change through the normal build/deploy
cycle acceptable. (Place rows are *not* in that category — they reload on a TTL, no restart; see
`core.md` §the place book.)

*How to apply:* never assume state survives a reschedule, and never put anything in it that can't be
rebuilt. **Every `state.set` is a Kafka record** — think about write frequency in a `decide` that runs
on a location stream — `validated_session_window` folds its bounding box into ten floats rather
than retaining fix bodies for exactly this reason.

## 21. The contract is generated, never hand-written

**`event.py` → `contracts/inferred_event.schema.json` → `dashboard/web/src/generated/events.ts`.**

*Why:* one source of truth for a shape shared across two languages.

*How to apply:* after changing `event.py`, run `scripts/emit_event_schema.py` and `npm run
gen:types`, and commit both. CI regenerates and fails on drift at each hop.

## 22. Documentation is updated with the behaviour it describes

**A change to `src/` that invalidates a claim in `core.md`, this file, or an ADR fixes it in the same
commit.**

*Why:* this repo's docs are load-bearing — they are the design record and the onboarding path for the
next session. The three docs deleted in 2026-07 spent months carrying "⚠️ STALE" banners because
this rule wasn't followed.
