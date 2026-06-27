# Inference Worker — Class Reference

> **⚠️ STALE (pre-Quix).** Most classes documented here (`Envelope`, `MessageBase`, engines, transport,
> pipeline, `RuntimeSupervisor`, …) belonged to the threaded runtime and **have been removed**
> (**superseded by [ADR 0004](adr/0004-scaling-model.md)**). The live runtime is
> [`inference.runtime.quix`](../src/inference/runtime/quix.py) (~150 lines) + `EventDefinition`
> ([`runtime/definition.py`](../src/inference/runtime/definition.py)). Read those directly; this doc
> awaits a rewrite.

## Events

### `Envelope` — `events/envelope.py`

Pydantic model for the metadata-wrapped event Vector publishes to Kafka. The transport layer parses every inbound message into an `Envelope`; engines read their data from `message`.

```python
class Envelope(BaseModel):
    event_name: str                     # canonical event id; routing metadata
    source_app: str                     # producer identity (e.g. "shortcut")
    source_type: str                    # how it was produced (e.g. "http_server")
    timestamp: datetime                 # wall-clock time of Vector ingestion (ISO 8601 on the wire)
    envelope_id: UUID                   # stable per-event id (Vector-minted); default_factory fallback
    message: SerializeAsAny[MessageBase]  # typed body, resolved from event_name via the registry
```

`message` carries the data — engines read `event_name`/`timestamp` from it (attribute access), never from the envelope-level fields. A `model_validator(mode="before")` resolves the raw message dict to its concrete `MessageBase` subclass via `MESSAGE_REGISTRY` (`OpaqueMessage` fallback). `SerializeAsAny` is required so subclass fields survive `model_dump_json` (the engine round-trips contributors through Redis). `Envelope.model_validate_json` raises `ValidationError` on a malformed payload → skip-and-commit.

### Messages — `events/messages.py`

`MessageBase(event_name, timestamp)` (strict, `extra="forbid"`); `OpaqueMessage` (`extra="allow"`) fallback for unregistered event types. Capability **mixins** + matching `@runtime_checkable` Protocols (Protocols for typing only — dispatch is nominal on the mixin): `GeoLocated`(`location: GeoPoint | None`)/`GeoLocatedP`, `Derived`(`derived_from: list[LineageRef]`)/`DerivedP`. `MESSAGE_REGISTRY` + `register(event_name)` decorator + `resolve_message_type(event_name)`. **No event registers a strict model today** — events are data (`events/*.yml`) and derived events emit a superset shape a strict model would reject, so everything resolves to `OpaqueMessage`. The registry/mixins remain the seam for typed/per-event models (open question in ADR 0003). Every emitted derived event carries `timestamp` (= int `occurred_at`) so it satisfies `MessageBase` and is windowable as a contributor to further derivations (ADR 0002).

---

## Protocols

### `Emitter` — `transport/protocol.py`

Structural protocol for result emission. Any class implementing `emit()` satisfies it — no inheritance required.

```python
def emit(self, event: dict) -> None
```

The transport layer calls `emit()` when the engine returns a result. The emitter has no knowledge of Kafka or the consumer.

---

### `InferenceEngine` — `engines/protocol.py`

Structural protocol. Any class implementing `decide()` satisfies it — no inheritance required. Swappable: `WeightedWindowEngine` is one implementation; a Bayesian (or other) engine is a drop-in.

```python
def decide(self, payload: Envelope) -> DerivedDraft | None
```

Accepts the parsed `Envelope`; returns a `DerivedDraft` (core + contributors) when the engine triggers, `None` when it does not. The engine decides and assembles the core only — message shaping is the enrichment pipeline's job. The engine has no knowledge of Kafka, the emitter, or the pipeline.

---

### `Enricher` — `pipeline/protocol.py`

Structural protocol (`@runtime_checkable`). Each enricher shapes one capability of a derived event.

```python
requires: type | None                          # capability a contributor's message must have (None = always)
def enrich(self, draft: DerivedDraft) -> DerivedDraft
```

Applicability is **declared**, not self-decided: the pipeline runs an enricher only when `requires is None` or some contributor's message `isinstance`s the required capability mixin (nominal). `enrich` is pure (returns a new draft via `model_copy(update=...)`, never mutates).

---

### `Observer` — `observers/protocol.py`

Structural protocol for observing engine lifecycle events.

```python
def on_start(self, topics: list[str]) -> None
def on_received(self, payload: Envelope) -> None
def on_inference(self, result: dict) -> None
def on_error(self, error: Exception, context: str | None = None) -> None
def on_shutdown(self) -> None
```

`on_start` fires once when `KafkaStreamHandler.start()` subscribes to topics; `on_shutdown` fires on SIGTERM/SIGINT.

---

## Concrete Classes

### `WeightedWindowEngine` — `engines/weighted_window.py`

Time-windowed, weighted threshold inference engine backed by Redis.

**Constructor:**
```python
WeightedWindowEngine(rules: dict, redis_config: dict | None = None)
```

`redis_config` is optional. When omitted (the production default), the engine builds its own connection from `REDIS_HOST` / `REDIS_PORT` / `REDIS_DB` / `REDIS_USERNAME` / `REDIS_PASSWORD` env vars via `_redis_config_from_env()`. Tests may pass an explicit dict to inject a fake Redis. See the **Engine-Owned Infrastructure** invariant.

**`rules` keys:**

| Key | Type | Description |
|---|---|---|
| `name` | `str` | Engine identifier; used as Redis key prefix and inference type |
| `weights` | `dict[str, int]` | Map of event name → weight |
| `threshold` | `int` | Minimum total score to trigger an inference |
| `window_seconds` | `int` | How far back to look for contributing events |
| `cooldown_seconds` | `int` | How long to suppress re-triggering after a successful inference (default: 1800) |

**Algorithm (`decide`):**
1. Drop the event if `payload.message["event_name"]` is not in `weights` (no Redis hit)
2. Add the event to a Redis ZSET scored by timestamp, and store its full body in a parallel Redis HASH (`inference:<name>:contributors`)
3. Prune the ZSET to `window_seconds`; prune the HASH to the surviving members
4. Fetch all active entries; deduplicate by event type keeping the earliest occurrence
5. Sum weights of unique contributors
6. If score ≥ threshold and no cooldown lock is active, take the lock atomically via `SET NX EX`
7. Fetch the contributing bodies and return a `DerivedDraft` (core + `contributors`). No message shaping happens here.

The ZSET/HASH/lock are private to this engine (see the **Engine-Owned Infrastructure** invariant); they are not part of the engine Protocol.

**Returns** a `DerivedDraft` (`pipeline/draft.py`) — core (`event_name`, `confidence_score`, `occurred_at`) + `contributors: tuple[Envelope, ...]`, the contributing source events as their full `Envelope`s (so enrichers get the message body, source metadata, and — once minted — `envelope_id`). Shaping (`sources`/`evidence`/`derived_from`/`location`) is the pipeline's job, not the engine's.

---

### `EnrichmentPipeline` / `finalize` — `pipeline/runner.py`

`EnrichmentPipeline(enrichers: list[Enricher])` folds the enrichers over the draft — skipping any whose declared `requires` capability no contributor satisfies (`_applies`), and best-effort around the rest (a raising enricher is logged and skipped) — then `finalize(draft)` returns the transport dict:

```python
{
    "inference_type": "...",   # metadata
    "processed_at": 1234.0,   # metadata — wall-clock at finalize
    "message": {
        "event_name": "...",
        "confidence_score": 12,
        "occurred_at": 1777673675.0,
        "sources": ["event_a", "event_b"],          # reconstructed from contributors
        "evidence": {"event_a": ..., "event_b": ...},# reconstructed from contributors
        "derived_from": [...],                       # added by LineageEnricher
        # "location": {...}                          # added by GeoEnricher iff applicable
    },
}
```

`sources`/`evidence` are reconstructed in `finalize` so the payload stays a superset of the pre-pipeline output. The dict is POSTed to Vector, which re-wraps it in an `Envelope` for `high_level_events`.

**Enrichers** (`pipeline/enrichers/`): `LineageEnricher` (`requires=None` → always; emits `derived_from` with real Vector-minted `envelope_id`s), `GeoEnricher(strategy="centroid")` (`requires=GeoLocated` → sets `location` centroid only when a contributor's message is a registered `GeoLocated` type — dormant until such a raw type is registered).

---

### `KafkaStreamHandler` — `transport/kafka_handler.py`

Drives the Kafka consumer loop and delegates to the engine, pipeline, and emitter.

**Constructor:**
```python
KafkaStreamHandler(
    kafka_consumer: Consumer,
    engine: InferenceEngine,
    observer: Observer,
    emitter: Emitter,
    pipeline: EnrichmentPipeline,
)
```

Per message: `engine.decide(envelope)` → if a draft is returned, `pipeline.run(draft)` → `emitter.emit(result)`.

**`start(source_topics: list[str])`** — blocking; exits cleanly on SIGTERM/SIGINT.

Commit strategy: `enable.auto.commit=False`. Offsets are committed manually after every message — including malformed messages, engine failures, and emit failures (skip-and-move-on).

---

### `InferenceObserver` — `observers/logging_observer.py`

Logging implementation of the `Observer` protocol. Uses Python's standard `logging` module with K8s-friendly structured output.

**Constructor:**
```python
InferenceObserver(name: str)
```

`name` becomes the logger name — use `rules["name"]` so log lines identify which engine worker produced them.

---

### `VectorHttpEmitter` — `transport/vector_http_emitter.py`

Emitter implementation that HTTP POSTs the inference result to a Vector `http_server` source.

**Constructor:**
```python
VectorHttpEmitter(url: str)
```

`url` is the Vector endpoint (e.g. `http://vector:8080`). Uses `urllib.request` with a 5-second timeout. Non-2xx responses raise an exception, which `KafkaStreamHandler` catches and logs as `"Emit failed"` before committing the offset and continuing.
