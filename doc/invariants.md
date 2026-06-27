# Inference Worker — Invariants

> **⚠️ PARTIALLY STALE (pre-Quix).** Several rules here (Redis-key identity, engine-owned Redis,
> transport adapters, single-writer-via-Lua) targeted the threaded runtime, **superseded by
> [ADR 0004](adr/0004-scaling-model.md)**. Under the Quix runtime, single-writer-per-key is structural
> (partition ownership, no Lua), state is Quix `State` (no Redis), and there is one shared consumer group
> (not one per event). The *identity* rule (definition `name` = emitted `event_name`) still holds. Awaits
> reconciliation; read ADR 0004 + [`quix.py`](../src/inference/runtime/quix.py) for current truth.

Design rules that must hold across the entire codebase. When adding new engines, observers, or transport adapters, verify these still hold.

---

## Payload Structure

**`message` contains data. Everything outside `message` is metadata.**

The inbound schema is enforced by the pydantic `Envelope` model in `src/inference/events/envelope.py`. Vector wraps every event in this shape before publishing to Kafka:

```python
Envelope(
    event_name="...",          # metadata — routing/filtering hint
    source_app="shortcut",    # metadata
    source_type="http_server",# metadata — Vector source type
    timestamp=datetime(...),  # metadata — wall-clock time of Vector ingestion
    envelope_id=UUID(...),    # metadata — stable per-event id, minted by Vector at ingest
    message={...},            # data — the original event body (untyped dict)
)
```

`message` is a typed `MessageBase` (resolved from `event_name` via `MESSAGE_REGISTRY`, falling back to `OpaqueMessage` for unregistered types). Its canonical data fields:
- `event_name: str` — canonical event identifier
- `timestamp: int` — Unix integer, used for windowing

Engines must read `event_name` and `timestamp` from `payload.message` (attribute access), never from the envelope-level fields. Envelope-level `event_name` and `timestamp` are transport/routing metadata only.

**Derived events are valid contributors (recursive derivation, ADR 0002).** A derived event emitted to `high_level_events` must itself satisfy this contract so a downstream worker can consume it as a contributor — in particular it must carry `message.timestamp` (the window key). `finalize()` sets `timestamp = int(occurred_at)` on every derived event for this reason. A worker derives from another derived event simply by listing `high_level_events` in its `source_topics` (e.g. `got_into_the_car` ← `car_door_opened` + `device_connected_to_power`). The graph must stay a DAG (no cycles) — the engine's `event_name` gatekeeper prevents a handler from re-deriving its own output even when it consumes the topic it emits to.

**`envelope_id`:** minted by Vector's `enrich_sensor` transform (`uuid_v4()`) for sensor events at ingest — the stable identity used for lineage (`derived_from` joins on it) and, later, persistence. The model carries a `default_factory` fallback so an event without one still parses, but Vector's id is authoritative.

**Capabilities are nominal.** Cross-cutting traits are capability **mixins** (`GeoLocated`, `Derived`); a concrete registered message inherits them. Detection is `isinstance(msg, GeoLocated)` against the mixin — **not** the `@runtime_checkable` Protocol (`OpaqueMessage` with `extra="allow"` would structurally false-match a stray `location` key). `Envelope.message` is typed `SerializeAsAny[MessageBase]` — `SerializeAsAny` is required so subclass fields survive `model_dump_json` (the engine round-trips contributors through Redis).

---

## Engine Contract

**`decide()` accepts an `Envelope` and returns a `DerivedDraft | None`. The engine decides and assembles the core; it does not shape the message.**

- `None` → no inference triggered; transport commits and moves on
- `DerivedDraft` → inference triggered. The draft carries only the **core** (`event_name`, `confidence_score`, `occurred_at`) plus the **contributors** (the source events, with their full bodies). The engine does *not* build `sources`/`evidence`/`location` or any capability-specific shape.

`InferenceEngine` is a swappable Protocol — `WeightedWindowEngine` is one implementation; others (e.g. a Bayesian engine) may follow. The gatekeeper, time window, weights, cooldown lock, and Redis are implementation details of `WeightedWindowEngine`, never part of the engine Protocol or of `DerivedDraft`. The engine has no reference to the Kafka producer, consumer, observer, or emitter.

See `doc/adr/0001-message-shaping-pipeline.md` for the design rationale and the future-state target.

---

## Enrichment Pipeline

**The message is shaped by an ordered chain of enrichers, not by the engine.**

After the engine returns a `DerivedDraft`, the worker runs it through an `EnrichmentPipeline` — an ordered list of `Enricher`s (`enrich(draft) -> draft`) configured per-worker in `main.py` next to `RULES` (the list sets availability + order + config). Each enricher:

- owns exactly **one** capability and **declares applicability** via `requires: type | None` (the capability mixin a contributor's message must be an instance of, or `None` = always). The **pipeline** evaluates `requires` centrally (`requires is None or any(isinstance(c.message, requires) for c in contributors)`) and only calls `enrich` when it applies — the enricher never self-decides whether to run;
- is **pure**: returns a new draft via `model_copy(update=...)`, never mutates the input;
- is judged on the **contributors** (a derived event gains a capability only if its contributors support it).

The pipeline is **best-effort**: by the time it runs, the engine has already decided to fire (possibly with irreversible side effects), so a raising enricher is logged and skipped — the event is still emitted, partially enriched. `finalize()` then merges the core + accreted capability fields into the transport dict; the Emitter still receives a `dict`, which Vector re-wraps into an `Envelope` for `high_level_events`.

**Contributor data:** because enrichers shape the derived event from its contributors, the engine must supply the contributing source events as full `Envelope`s in the draft (`DerivedDraft.contributors: tuple[Envelope, ...]`), not a flattened subset. *How* it retains them is engine-private (`WeightedWindowEngine` keeps the full envelopes in a Redis HASH pruned alongside its ZSET).

---

## Engine-Owned Infrastructure

**Engines own their storage and connection dependencies. The worker layer never plumbs them through.**

If an engine needs Redis, Postgres, or any other backend, it resolves that connection itself (typically via a helper in the engine module that reads env vars). `config.py` exposes only the infrastructure that the wiring layer needs directly — Kafka, SSL certs, Vector. Engine-internal config (e.g. `REDIS_*` env vars) is read by the engine itself and never appears in `config.py` or in `main.py`.

**Why:** Different engines may use different backends. Forcing every backend through `config.py` and `main.py` creates a leaky abstraction where the worker has to know each engine's internal implementation. Swapping engines should be a localized change to the worker's import + instantiation lines — not a ripple through shared config.

**How to apply:** A worker constructing an engine should never pass connection config (`redis_config=...`, `db_url=...`, etc.). It passes only the engine's *logical* config — rules, thresholds, weights. For testability, engine `__init__` may accept an optional override (`redis_config: dict | None = None`); production code passes nothing.

---

## Transport Contract

**The transport layer is unaware of Redis or any engine internals.**

`KafkaStreamHandler` receives a consumer, an engine, an observer, and an emitter. It does not know how the engine works — only that `process()` returns a result or `None`. It does not know where the emitter sends results — only that `emit()` accepts a dict.

---

## Commit Strategy

**Offsets are committed manually after every message, including errors.**

`enable.auto.commit=False`. The handler commits after:
- Successful processing (with or without an inference trigger)
- JSON decode errors (skip-and-move-on)
- Engine exceptions (skip-and-move-on)

A message is never retried indefinitely. If a message cannot be processed, it is logged and skipped.

---

## Cooldown Lock Atomicity

**The cooldown lock must be set atomically.**

`SET NX EX` (atomic) is used instead of a separate `EXISTS` + `SETEX`. This prevents two concurrent engine instances from both passing the threshold check and both emitting a duplicate inference within the same cooldown window.

---

## Single-Writer Per Event Key

**Each event's Redis state has exactly one writer. An event scales to `replicas > 1` only after its engine's decide-path is made atomic.**

`WeightedWindowEngine` derives all its keys from the event `name` alone — `inference:<name>:buffer` (ZSET), `inference:<name>:contributors` (HASH), `inference:<name>:lock`. There is no per-pod or per-partition component, so **every consumer of the same event resolves to the same keys**. For a *global* correlation window this is correct and intended: state must be centralized, or partitioning the source topics across consumers would fragment the window and drop co-occurrences whose contributors landed on different partitions. The shared ZSET is the right design — the constraint is on *concurrent access* to it, not on sharing it.

The hazard is that `decide()` is only **partially** atomic:

- The opening pipeline (`zadd` + `hset` + `zremrangebyscore` + `zrange`) runs as one `MULTI`/`EXEC` (redis-py `transaction=True` default) — atomic. ✅
- The **HASH prune that follows is not transactional**: it computes `stale = hkeys() - survivors` against the ZRANGE snapshot it captured a moment earlier, then `hdel`s the difference. A second writer that `ZADD`s + `HSET`s a new member *between* the snapshot and the `hkeys()` call will have its live member treated as stale and its body deleted — the ZSET keeps the member, the HASH loses its body, and a later `hmget` at fire time silently drops that contributor. Concurrency-only, silent data loss.

The cooldown `SET NX EX` (see above) *does* hold under concurrency, so the worst case is a lost/partial contributor body, **not** a double-fire.

**Why it's safe today:** the current topology runs exactly one handler per event in one process, with no replicas in practice — so there is never a second concurrent writer and the non-atomic prune is fine. This is a load-bearing assumption that was previously unstated.

**How to apply:**
- Treat "one consumer per event key" as an invariant of the engine as written. Single-replica-per-event is the safe default.
- Before setting `replicas > 1` for any event (i.e. intra-event throughput scaling via Kafka partition fan-out), the engine's whole read-modify-decide-prune cycle must first be made atomic — fold it into a single server-side `EVAL` (Lua) script, or guard it with a short per-key lock. Once atomic, replicas scale that event's throughput correctly because the shared ZSET is exactly the state they should share.
- Event-level horizontal scaling (one Deployment per event / per shard) and intra-event scaling (`replicas > 1` for a hot event) are **separate axes**. The first needs no engine change; the second is gated on this atomicity fix.

---

## Configuration Source

**Shared infra config lives in `config.py`. Per-event config lives in `events/<name>.yml`. Engine-internal infra lives in the engine module.**

> Under ADR 0003 an event is data, not a directory: each `events/<name>.yml` is an `EventDefinition` and the generic runtime loads them all. This section reflects that; the old per-`workers/<name>/main.py` identity rule (`WORKER_NAME = Path(__file__).parent.name`) is retired.

- `config.py` holds only the cluster-shared infrastructure that the wiring layer touches directly: Kafka bootstrap servers, SSL cert paths, Vector base URL. Sourced from env vars / K8s Secrets and identical across all events.
- Each `events/<name>.yml` declares its own config: `engine` + `engine_config` (threshold, window, weights, …), `source_topics`, `sink_topic`, `event_domain`, `enrichers`. Event-specific, no meaningful shared default.
- The event's identity must be derived from the definition's `name` field, never declared piecemeal. It is the single source of truth; this prevents drift across the data and infra layers. Two forms:
  - `name` (snake_case) — **data layer**: `RULES["name"]`, Redis keys, emitted `inference_type`, Vector URL path (`{domain}/{name}/{sink}`), logger names.
  - `slug = name.replace("_", "-")` (kebab-case) — **infra layer**: Kafka consumer group ID (`inference-<slug>-v1`), and any other external-naming boundary that rejects underscores.
- Engines and enrichers are resolved from the definition's string keys via the registries in `runtime/registry.py`; concrete implementations self-register, so the runtime/framework names none of them.
- Engine-internal infra (Redis connection, future Postgres connection, etc.) lives in the engine module — see **Engine-Owned Infrastructure**. It must not appear in `config.py` or the runtime/wiring.

The runtime `main.py` is only wiring — it does not contain logic. The `events/*.yml` definitions are the per-event ConfigMap-equivalent (and in ADR 0003 Phase 2 literally become a ConfigMap).
